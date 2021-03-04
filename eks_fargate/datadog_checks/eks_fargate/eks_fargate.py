# (C) Datadog, Inc. 2020
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import logging
import os

from datadog_checks.base import AgentCheck
from datadog_checks.base.utils.tagging import tagger

log = logging.getLogger('collector')

KUBELET_NODE_ENV_VAR = "DD_KUBERNETES_KUBELET_NODENAME"
HOSTNAME_ENV_VAR = "HOSTNAME"
CAPACITY_ENV_VAR = "DD_EKS_CAPACITY_ANNOTATION"


class EksFargateCheck(AgentCheck):
    """
    Generate heartbeat and capacity metrics for Amazon EKS on AWS Fargate workloads
    """

    def __init__(self, name, init_config, instances):
        super(EksFargateCheck, self).__init__(name, init_config, instances)
        self.NAMESPACE = "eks.fargate"
        pod_name = os.getenv(HOSTNAME_ENV_VAR)
        pod_id = os.getenv("DD_POD_UID", "")
        virtual_node = os.getenv(KUBELET_NODE_ENV_VAR, "")
        self.fargate_mode = pod_name is not None and "fargate" in virtual_node

        self.tags = set()

        if self.fargate_mode:
            self.tags.update(["pod_name:" + pod_name, "virtual_node:" + virtual_node])
            self.tags.update(instances[0].get("tags", []))
            self.tags.update(tagger.tag("kubernetes_pod_uid://%s" % pod_id, tagger.LOW) or [])
            self.tags.update(os.getenv("DD_KUBERNETES_POD_LABELS_AS_TAGS", []))

    def check(self, instance):
        if self.fargate_mode:
            # Submit the heartbeat metric for fargate virtual nodes.
            self.gauge(self.NAMESPACE + ".pods.running", 1, self.tags)

            # Submit metrics for resource capacities
            capacity_annotation = os.getenv(CAPACITY_ENV_VAR, "")
            if capacity_annotation != "":
                cpu_val, mem_val = extract_resource_values(capacity_annotation)
                if cpu_val == 0 and mem_val == 0:
                    return
                self.gauge(self.NAMESPACE + ".capacity.cpu", cpu_val, self.tags)
                self.gauge(self.NAMESPACE + ".capacity.memory", mem_val, self.tags)


def extract_resource_values(capacity_annotation):
    # Given pod annotations, extract the resource values for submission as metrics
    # "0.25vCPU 0.5GB"
    cpu_val, mem_val = 0, 0
    capacities = capacity_annotation.split(" ")
    if len(capacities) != 2:
        return cpu_val, mem_val
    cpu, mem = capacities
    if cpu.endswith("vCPU"):
        cpu_val = float(cpu.strip("vCPU"))
    if mem.endswith("GB"):
        mem_val = float(mem.strip("GB"))
    return cpu_val, mem_val
