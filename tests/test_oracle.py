import time
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock

import oci

from hunter.core import AttentionRequired, Budget, PREFERRED, SMALLER
from hunter.oracle import BOOT_GB, OWNER_TAG, SHAPE, OracleGateway


class OracleTests(unittest.TestCase):
    def gateway(self):
        gateway = OracleGateway.__new__(OracleGateway)
        gateway.oci = oci
        gateway.budget = Budget(720)
        gateway.no_retry = oci.retry.NoneRetryStrategy()
        gateway.tenancy = gateway.compartment = "root"
        gateway.ssh_key = "ssh-ed25519 public-test-key"
        gateway.compartments = ["root", "child"]
        gateway.ads = ["AD-1", "AD-2", "AD-3"]
        gateway.subnets = {ad: "subnet" for ad in gateway.ads}
        gateway.image_id = "image"
        gateway.compute = NS(
            list_instances="instances",
            list_images="images",
            list_shapes="shapes",
            launch_instance="launch",
            get_instance="get_instance",
        )
        gateway.network = NS(list_vcns="vcns", list_subnets="subnets")
        gateway.storage = NS(list_volumes="volumes", list_boot_volumes="boot_volumes")
        gateway.containers = NS(list_container_instances="containers")
        gateway.limits = NS(get_resource_availability="quota")
        self.inventory = {
            "instances": [],
            "containers": [],
            "volumes": [],
            "boot_volumes": [],
            "vcns": [
                NS(id="vcn", display_name="tott-vcn", lifecycle_state="AVAILABLE")
            ],
            "subnets": [
                NS(
                    id="subnet",
                    availability_domain=None,
                    prohibit_public_ip_on_vnic=False,
                    lifecycle_state="AVAILABLE",
                )
            ],
            "images": [
                NS(id="image", lifecycle_state="AVAILABLE", compartment_id=None)
            ],
            "shapes": [NS(shape=SHAPE)],
        }
        gateway._list = Mock(
            side_effect=lambda operation, *args, **kwargs: self.inventory[operation]
        )
        gateway._call = Mock(return_value=NS(data=NS(available=12)))
        return gateway

    def test_regional_public_subnet_covers_all_domains(self):
        gateway = self.gateway()
        plan = gateway.preflight((PREFERRED,))
        self.assertEqual(len(plan.candidates), 3)
        self.assertEqual(gateway.subnets, {ad: "subnet" for ad in gateway.ads})

    def test_ad_specific_subnets_are_selected_per_domain(self):
        gateway = self.gateway()
        self.inventory["subnets"] = [
            NS(
                id=ad + "-subnet",
                availability_domain=ad,
                prohibit_public_ip_on_vnic=False,
                lifecycle_state="AVAILABLE",
            )
            for ad in gateway.ads
        ]
        gateway.preflight((PREFERRED,))
        self.assertEqual(gateway.subnets["AD-3"], "AD-3-subnet")

    def test_subnet_missing_for_other_domains_fails_preflight(self):
        gateway = self.gateway()
        self.inventory["subnets"][0].availability_domain = "AD-1"
        with self.assertRaisesRegex(AttentionRequired, "AD-2"):
            gateway.preflight((PREFERRED,))

    def test_private_subnet_and_ambiguous_subnets_are_rejected(self):
        gateway = self.gateway()
        self.inventory["subnets"][0].prohibit_public_ip_on_vnic = True
        with self.assertRaises(AttentionRequired):
            gateway.preflight((PREFERRED,))
        self.inventory["subnets"][0].prohibit_public_ip_on_vnic = False
        self.inventory["subnets"] *= 2
        with self.assertRaises(AttentionRequired):
            gateway.preflight((PREFERRED,))

    def test_quota_filters_one_domain_without_discarding_other_domains(self):
        gateway = self.gateway()
        gateway._call.side_effect = lambda operation, *args, **kwargs: NS(
            data=NS(available=0 if kwargs["availability_domain"] == "AD-2" else 12)
        )
        plan = gateway.preflight((PREFERRED, SMALLER))
        self.assertEqual({ad for ad, profile in plan.candidates}, {"AD-1", "AD-3"})

    def test_existing_storage_in_child_compartment_prevents_exceeding_200_gb(self):
        gateway = self.gateway()
        self.inventory["volumes"] = [NS(lifecycle_state="AVAILABLE", size_in_gbs=80)]
        # Both compartments contribute 80 GB, so another 50 GB would exceed the guard.
        with self.assertRaisesRegex(AttentionRequired, "200 GB"):
            gateway.preflight((PREFERRED,))

    def test_exact_requested_size_cannot_exceed_existing_a1_allocation(self):
        gateway = self.gateway()
        self.inventory["instances"] = [
            NS(
                id="other",
                shape=SHAPE,
                display_name="other",
                lifecycle_state="STOPPED",
                freeform_tags={},
                shape_config=NS(ocpus=0.5, memory_in_gbs=1),
            )
        ]
        with self.assertRaises(AttentionRequired):
            gateway.guard_before_launch((PREFERRED,))
        self.assertIsNone(gateway.guard_before_launch((SMALLER,)))

    def test_container_allocation_requires_allowance_review(self):
        gateway = self.gateway()
        self.inventory["containers"] = [
            NS(lifecycle_state="ACTIVE", shape="CI.Standard.A1.Flex")
        ]
        with self.assertRaisesRegex(AttentionRequired, "container"):
            gateway.preflight((PREFERRED,))

    def test_renamed_owned_instance_still_prevents_another_launch(self):
        gateway = self.gateway()
        gateway.compartments = ["root"]
        self.inventory["instances"] = [
            NS(
                id="vm",
                shape=SHAPE,
                display_name="renamed",
                availability_domain="AD-1",
                lifecycle_state="RUNNING",
                freeform_tags=OWNER_TAG,
            )
        ]
        self.assertEqual(gateway.preflight((PREFERRED,)).existing.id, "vm")

    def test_wrong_home_region_is_rejected(self):
        gateway = self.gateway()
        gateway.identity = NS(list_region_subscriptions="subscriptions")
        gateway._list = Mock(
            return_value=[NS(is_home_region=True, region_name="us-ashburn-1")]
        )
        with self.assertRaisesRegex(AttentionRequired, "home region"):
            gateway.verify_home()

    def pending(self, gateway):
        record = gateway.prepare("AD-1", PREFERRED)
        record.update(
            phase="pending", retry_token=str(uuid.uuid4()), started_at=time.time()
        )
        return record

    def test_expired_or_modified_pending_record_fails_closed(self):
        gateway = self.gateway()
        record = self.pending(gateway)
        gateway.validate_pending(record)
        record["request"]["memory_gb"] = 24
        with self.assertRaises(AttentionRequired):
            gateway.validate_pending(record)
        record = self.pending(gateway)
        record["started_at"] -= 24 * 3600
        with self.assertRaises(AttentionRequired):
            gateway.validate_pending(record)

    def test_changed_ssh_key_cannot_change_an_uncertain_request(self):
        gateway = self.gateway()
        record = self.pending(gateway)
        gateway.ssh_key = "new key"
        with self.assertRaises(AttentionRequired):
            gateway.validate_pending(record)

    def test_actual_sdk_launch_models_use_free_shape_and_bounded_boot_volume(self):
        gateway = self.gateway()
        record = self.pending(gateway)
        gateway._call.return_value = NS(
            data=NS(id="vm", lifecycle_state="PROVISIONING", availability_domain="AD-1")
        )
        gateway.launch(record)
        args, kwargs = gateway._call.call_args
        details = args[1]
        self.assertEqual(details.shape, SHAPE)
        self.assertEqual(details.shape_config.ocpus, 2)
        self.assertEqual(details.shape_config.memory_in_gbs, 12)
        self.assertEqual(details.source_details.boot_volume_size_in_gbs, BOOT_GB)
        self.assertEqual(details.source_details.boot_volume_vpus_per_gb, 10)
        self.assertIsNone(details.fault_domain)
        self.assertEqual(kwargs["opc_retry_token"], record["retry_token"])

    def test_every_sdk_call_disables_internal_retries(self):
        gateway = self.gateway()
        operation = Mock(return_value=NS(data=[]))
        OracleGateway._call(gateway, operation, "argument")
        self.assertIs(operation.call_args.kwargs["retry_strategy"], gateway.no_retry)

    def test_boot_volume_inventory_matches_actual_sdk_keyword_signature(self):
        gateway = self.gateway()
        # list_boot_volumes accepts no positional arguments in the pinned SDK.
        calls = [
            call
            for call in gateway._list.call_args_list
            if call.args[0] == "boot_volumes"
        ]
        self.assertEqual(calls, [])
        gateway._guard_allowance([], (PREFERRED,))
        calls = [
            call
            for call in gateway._list.call_args_list
            if call.args[0] == "boot_volumes"
        ]
        self.assertEqual(len(calls), 6)
        self.assertTrue(
            all(
                len(call.args) == 1 and "availability_domain" in call.kwargs
                for call in calls
            )
        )

    def test_pagination_does_not_drop_other_resources(self):
        gateway = self.gateway()
        gateway._call.side_effect = [
            NS(data=[1], headers={"opc-next-page": "next"}),
            NS(data=[2], headers={}),
        ]
        self.assertEqual(
            OracleGateway._list(gateway, "operation", "compartment"), [1, 2]
        )
        self.assertEqual(gateway._call.call_args.kwargs["page"], "next")

    def test_actual_container_collection_response_is_supported(self):
        gateway = self.gateway()
        collection = oci.container_instances.models.ContainerInstanceCollection(
            items=[]
        )
        gateway._call.return_value = NS(data=collection, headers={})
        self.assertEqual(OracleGateway._list(gateway, "containers", "root"), [])

    def test_missing_inventory_data_is_not_assumed_empty(self):
        gateway = self.gateway()
        gateway._call.return_value = NS(data=None, headers={})
        with self.assertRaises(AttentionRequired):
            OracleGateway._list(gateway, "containers", "root")


if __name__ == "__main__":
    unittest.main()
