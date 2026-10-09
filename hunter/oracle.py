"""OCI adapter with explicit free-tier, inventory, subnet, and request guards."""

import hashlib
import json
import os
import time
import uuid

from .core import AttentionRequired, Instance, Plan, PREFERRED, SMALLER

REGION = "eu-frankfurt-1"
SHAPE = "VM.Standard.A1.Flex"
INSTANCE_NAME = "tott-web"
BOOT_GB = 50
FREE_STORAGE_GB = 200
OWNER_TAG = {"oci-a1-hunter": "tott-web-v1"}


class OracleGateway:
    def __init__(self, budget, *, env=None):
        import oci

        self.oci = oci
        self.budget = budget
        env = os.environ if env is None else env
        names = [
            "OCI_CLI_USER",
            "OCI_CLI_TENANCY",
            "OCI_CLI_FINGERPRINT",
            "OCI_CLI_KEY_CONTENT",
            "OCI_CLI_REGION",
            "SSH_PUBLIC_KEY",
        ]
        missing = [name for name in names if not env.get(name, "").strip()]
        if missing:
            raise AttentionRequired("Missing required secrets: " + ", ".join(missing))
        if env["OCI_CLI_REGION"].strip() != REGION:
            raise AttentionRequired("This hunter is restricted to eu-frankfurt-1.")
        self.tenancy = env["OCI_CLI_TENANCY"].strip()
        self.compartment = (
            self.tenancy
        )  # Preserve the original root-compartment placement.
        self.ssh_key = env["SSH_PUBLIC_KEY"].strip()
        if (
            not self.ssh_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-"))
            or "\n" in self.ssh_key
        ):
            raise AttentionRequired(
                "SSH_PUBLIC_KEY must contain one OpenSSH public key."
            )
        from cryptography.hazmat.primitives.serialization import load_ssh_public_key

        try:
            load_ssh_public_key(self.ssh_key.encode())
        except (ValueError, TypeError) as exc:
            raise AttentionRequired(
                "SSH_PUBLIC_KEY is not a valid OpenSSH public key."
            ) from exc
        config = {
            "user": env["OCI_CLI_USER"].strip(),
            "tenancy": self.tenancy,
            "fingerprint": env["OCI_CLI_FINGERPRINT"].strip(),
            "region": REGION,
            "key_content": env["OCI_CLI_KEY_CONTENT"].strip(),
        }
        oci.config.validate_config(config)
        self.no_retry = oci.retry.NoneRetryStrategy()
        kwargs = {
            "timeout": (5, 20),
            "retry_strategy": self.no_retry,
            "circuit_breaker_strategy": oci.circuit_breaker.NoCircuitBreakerStrategy(),
        }
        self.identity = oci.identity.IdentityClient(config, **kwargs)
        self.compute = oci.core.ComputeClient(config, **kwargs)
        self.network = oci.core.VirtualNetworkClient(config, **kwargs)
        self.storage = oci.core.BlockstorageClient(config, **kwargs)
        self.containers = oci.container_instances.ContainerInstanceClient(
            config, **kwargs
        )
        self.limits = oci.limits.LimitsClient(config, **kwargs)
        self.compartments = []
        self.ads = []
        self.subnets = {}
        self.image_id = None

    def _call(self, operation, *args, **kwargs):
        self.budget.require(25)
        return operation(*args, retry_strategy=self.no_retry, **kwargs)

    def _list(self, operation, *args, **kwargs):
        items = []
        page = None
        while True:
            response = self._call(
                operation, *args, **kwargs, **({"page": page} if page else {})
            )
            # Older APIs return arrays; Container Instances returns a collection model.
            batch = response.data
            if not isinstance(batch, list):
                batch = getattr(batch, "items", None)
            if not isinstance(batch, list):
                raise AttentionRequired(
                    "OCI returned an unrecognized inventory response; stopping safely."
                )
            items.extend(batch)
            page = response.headers.get("opc-next-page")
            if not page:
                return items

    def verify_home(self):
        subscriptions = self._list(
            self.identity.list_region_subscriptions, self.tenancy
        )
        if not any(s.is_home_region and s.region_name == REGION for s in subscriptions):
            raise AttentionRequired(
                "Frankfurt is not the tenancy home region; stopping to keep compute free."
            )
        compartments = self._list(
            self.identity.list_compartments,
            self.tenancy,
            compartment_id_in_subtree=True,
            access_level="ANY",
        )
        self.compartments = [self.tenancy] + [
            c.id for c in compartments if c.lifecycle_state == "ACTIVE"
        ]
        self.ads = [
            ad.name
            for ad in self._list(self.identity.list_availability_domains, self.tenancy)
        ]
        if not self.ads:
            raise AttentionRequired("OCI returned no availability domains.")

    def _instances(self):
        return [
            instance
            for compartment in self.compartments
            for instance in self._list(self.compute.list_instances, compartment)
            if instance.lifecycle_state != "TERMINATED"
        ]

    @staticmethod
    def _instance(instance):
        return Instance(
            instance.id, instance.lifecycle_state, instance.availability_domain
        )

    def find_existing(self, inventory=None):
        inventory = self._instances() if inventory is None else inventory
        matches = [
            i
            for i in inventory
            if i.display_name == INSTANCE_NAME
            or (i.freeform_tags or {}).get("oci-a1-hunter")
            == OWNER_TAG["oci-a1-hunter"]
        ]
        if len(matches) > 1:
            raise AttentionRequired(
                "Multiple matching instances exist; inspect OCI before continuing."
            )
        return self._instance(matches[0]) if matches else None

    def get_instance(self, instance_id):
        return self._instance(self._call(self.compute.get_instance, instance_id).data)

    def _guard_allowance(self, inventory, profiles):
        used_cpu = used_memory = 0
        for instance in inventory:
            if instance.shape == SHAPE or instance.shape.startswith("BM.Standard.A1"):
                config = instance.shape_config
                if not config or config.ocpus is None or config.memory_in_gbs is None:
                    raise AttentionRequired("Cannot determine existing A1 allocation.")
                used_cpu += config.ocpus
                used_memory += config.memory_in_gbs
        # A1 container instances share compute quotas; refuse to assume they use no allocation.
        for compartment in self.compartments:
            containers = self._list(
                self.containers.list_container_instances, compartment
            )
            if any(
                c.lifecycle_state != "DELETED" and "Standard.A1" in c.shape
                for c in containers
            ):
                raise AttentionRequired(
                    "Existing A1 container allocation needs manual allowance review."
                )
        usable = [
            p
            for p in profiles
            if used_cpu + p.ocpus <= 2 and used_memory + p.memory_gb <= 12
        ]
        if not usable:
            raise AttentionRequired(
                "Existing A1 allocation leaves no room within the 2 OCPU / 12 GB guard."
            )
        used_storage = 0
        for compartment in self.compartments:
            volumes = self._list(self.storage.list_volumes, compartment_id=compartment)
            volumes += [
                v
                for ad in self.ads
                for v in self._list(
                    self.storage.list_boot_volumes,
                    availability_domain=ad,
                    compartment_id=compartment,
                )
            ]
            for volume in volumes:
                if volume.lifecycle_state != "TERMINATED":
                    if volume.size_in_gbs is None:
                        raise AttentionRequired(
                            "Cannot determine existing block/boot volume allocation."
                        )
                    used_storage += volume.size_in_gbs
        if used_storage + BOOT_GB > FREE_STORAGE_GB:
            raise AttentionRequired(
                f"Existing volumes use {used_storage} GB; another {BOOT_GB} GB would exceed the 200 GB guard."
            )
        return usable

    def guard_before_launch(self, profiles):
        inventory = self._instances()
        existing = self.find_existing(inventory)
        if not existing:
            # Recheck inventories; fail closed on any permission/read error.
            self._guard_allowance(inventory, profiles)
        return existing

    def preflight(self, profiles):
        if any(p not in {PREFERRED, SMALLER} for p in profiles):
            raise AttentionRequired(
                "Only the bounded 2/12 and optional 1/6 A1 configurations are supported."
            )
        inventory = self._instances()
        existing = self.find_existing(inventory)
        if existing:
            return Plan([], existing)
        usable = self._guard_allowance(inventory, profiles)
        vcns = [
            v
            for v in self._list(self.network.list_vcns, self.compartment)
            if v.display_name == "tott-vcn" and v.lifecycle_state == "AVAILABLE"
        ]
        if len(vcns) != 1:
            raise AttentionRequired(
                "Expected exactly one available tott-vcn in the root compartment."
            )
        subnets = [
            s
            for s in self._list(
                self.network.list_subnets, self.compartment, vcn_id=vcns[0].id
            )
            if s.lifecycle_state == "AVAILABLE" and not s.prohibit_public_ip_on_vnic
        ]
        for ad in self.ads:
            compatible = [s for s in subnets if s.availability_domain in {None, ad}]
            regional = [s for s in compatible if s.availability_domain is None]
            choices = regional or compatible
            if len(choices) != 1:
                raise AttentionRequired(
                    f"Expected one compatible public subnet for {ad}; found {len(choices)}. "
                    "Use one regional public subnet, or one public subnet per domain."
                )
            self.subnets[ad] = choices[0].id
        images = self._list(
            self.compute.list_images,
            self.compartment,
            operating_system="Canonical Ubuntu",
            operating_system_version="22.04",
            shape=SHAPE,
            sort_by="TIMECREATED",
            sort_order="DESC",
        )
        images = [
            i
            for i in images
            if i.lifecycle_state == "AVAILABLE" and i.compartment_id is None
        ]
        if not images:
            raise AttentionRequired(
                "No available platform Ubuntu 22.04 image compatible with A1."
            )
        self.image_id = images[0].id
        candidates = []
        for ad in self.ads:
            shapes = self._list(
                self.compute.list_shapes,
                self.compartment,
                availability_domain=ad,
                image_id=self.image_id,
            )
            if not any(s.shape == SHAPE for s in shapes):
                continue
            available = {}
            for name in ("standard-a1-core-count", "standard-a1-memory-count"):
                availability = self._call(
                    self.limits.get_resource_availability,
                    "compute",
                    name,
                    self.compartment,
                    availability_domain=ad,
                ).data
                if availability.available is None:
                    raise AttentionRequired(
                        f"OCI did not report effective quota for {name} in {ad}."
                    )
                available[name] = availability.available
            candidates.extend(
                (ad, p)
                for p in usable
                if p.ocpus <= available["standard-a1-core-count"]
                and p.memory_gb <= available["standard-a1-memory-count"]
            )
        if not candidates:
            raise AttentionRequired(
                "No compatible A1 domain has enough effective compute quota."
            )
        return Plan(candidates)

    def _fingerprint(self, snapshot):
        # Public state retains immutable image/subnet/size choices, never the SSH key or API key.
        payload = {
            "snapshot": snapshot,
            "compartment": self.compartment,
            "name": INSTANCE_NAME,
            "shape": SHAPE,
            "boot_gb": BOOT_GB,
            "vpus_per_gb": 10,
            "region": REGION,
            "assign_public_ip": True,
            "freeform_tags": OWNER_TAG,
            "ssh_sha256": hashlib.sha256(self.ssh_key.encode()).hexdigest(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def prepare(self, ad, profile):
        snapshot = {
            "availability_domain": ad,
            "ocpus": profile.ocpus,
            "memory_gb": profile.memory_gb,
            "subnet_id": self.subnets[ad],
            "image_id": self.image_id,
        }
        return {"request": snapshot, "fingerprint": self._fingerprint(snapshot)}

    def validate_pending(self, record):
        try:
            snapshot = record["request"]
            profile = (snapshot["ocpus"], snapshot["memory_gb"])
            token = str(uuid.UUID(record["retry_token"]))
            age = time.time() - record["started_at"]
            valid = (
                snapshot["availability_domain"] in self.ads
                and profile in {(2, 12), (1, 6)}
                and token == record["retry_token"]
                and 0 <= age < 23 * 3600
                and record["fingerprint"] == self._fingerprint(snapshot)
            )
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise AttentionRequired(
                "Pending launch is expired, malformed, or its configuration changed. "
                "Inspect OCI and the checkpoint before resetting it; no fresh launch is safe."
            )

    def launch(self, record):
        self.validate_pending(record)
        s = record["request"]
        details = self.oci.core.models.LaunchInstanceDetails(
            availability_domain=s["availability_domain"],
            compartment_id=self.compartment,
            display_name=INSTANCE_NAME,
            shape=SHAPE,
            shape_config=self.oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=s["ocpus"], memory_in_gbs=s["memory_gb"]
            ),
            source_details=self.oci.core.models.InstanceSourceViaImageDetails(
                image_id=s["image_id"],
                boot_volume_size_in_gbs=BOOT_GB,
                boot_volume_vpus_per_gb=10,
            ),
            create_vnic_details=self.oci.core.models.CreateVnicDetails(
                subnet_id=s["subnet_id"], assign_public_ip=True
            ),
            metadata={"ssh_authorized_keys": self.ssh_key},
            freeform_tags=OWNER_TAG,
        )
        response = self._call(
            self.compute.launch_instance, details, opc_retry_token=record["retry_token"]
        )
        return self._instance(response.data)
