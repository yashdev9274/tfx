# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Kubernetes TFX runner for out-of-cluster orchestration."""

import absl
import datetime
import json
import time
from typing import Dict, List, Text

from tfx.components.base import base_node
from tfx.orchestration import pipeline as tfx_pipeline
from tfx.orchestration.kubeflow import node_wrapper
from tfx.utils import json_utils, kube_utils

from google.protobuf import json_format
from kubernetes import client


_ORCHESTRATOR_COMMAND = [
    'python', '-m', 'tfx.orchestration.experimental.kubernetes.orchestrator_container_entrypoint'
]

# Number of seconds to wait for a Kubernetes job to spawn a pod.
# This is expected to take only a few seconds.
JOB_CREATION_TIMEOUT = 300


def run_as_kubernetes_job(pipeline: tfx_pipeline.Pipeline,
                          tfx_image: Text) -> None:
  """Submits and runs a TFX pipeline from outside the cluster.

  Args:
    pipeline: Logical pipeline containing pipeline args and components.
  """

  # TODO(ccy): Look for alternative serialization schemes once available.
  serialized_pipeline = _serialize_pipeline(pipeline)
  arguments = [
      '--serialized_pipeline',
      serialized_pipeline,
      '--tfx_image',
      tfx_image,
  ]
  batch_api = kube_utils.make_batch_v1_api()
  job_name = 'Job_' + pipeline.pipeline_info.run_id
  pod_label = kube_utils.sanitize_pod_name(job_name)
  container_name = 'pipeline-orchestrator'
  job = kube_utils.make_job_object(
      name=job_name,
      container_image=tfx_image,
      command=_ORCHESTRATOR_COMMAND + arguments,
      container_name=container_name,
      pod_labels={
          'job-name': pod_label,
      },
  )
  try:
    batch_api.create_namespaced_job(
        'default', job, pretty=True)
  except client.rest.ApiException as e:
    raise RuntimeError('Failed to submit job! \nReason: %s\nBody: %s' %
                       (e.reason, e.body))

  # Wait for pod to start.
  orchestrator_pods = []
  core_api = kube_utils.make_core_v1_api()
  start_time = datetime.datetime.utcnow()

  # Wait for the kubernetes job to launch a pod.
  while not orchestrator_pods and (
      datetime.datetime.utcnow() - start_time).seconds < JOB_CREATION_TIMEOUT:
    try:
      orchestrator_pods = core_api.list_namespaced_pod(
          namespace='default',
          label_selector='job-name={}'.format(pod_label)
      ).items
    except client.rest.ApiException as e:
      if e.status != 404:
        raise RuntimeError('Unknown error! \nReason: %s\nBody: %s' %
                           (e.reason, e.body))
    time.sleep(1)

  # Transient orchestrator should only have 1 pod.
  if len(orchestrator_pods) != 1:
    raise RuntimeError('Expected 1 pod launched by Kubernetes job, found %d' %
                       len(orchestrator_pods))
  orchestrator_pod = orchestrator_pods.pop()
  pod_name = orchestrator_pod.metadata.name

  absl.logging.info('Waiting for pod "default:%s" to start.', pod_name)
  kube_utils.wait_pod(
      core_api,
      pod_name,
      'default',
      exit_condition_lambda=kube_utils.pod_is_not_pending,
      condition_description='non-pending status')

  # Stream logs from orchestrator pod.
  absl.logging.info('Start log streaming for pod "default:%s".', pod_name)
  try:
    logs = core_api.read_namespaced_pod_log(
        name=pod_name,
        namespace='default',
        container=container_name,
        follow=True,
        _preload_content=False).stream()
  except client.rest.ApiException as e:
    raise RuntimeError(
        'Failed to stream the logs from the pod!\nReason: %s\nBody: %s' %
        (e.reason, e.body))

  for log in logs:
    absl.logging.info(log.decode().rstrip('\n'))

  resp = kube_utils.wait_pod(
      core_api,
      pod_name,
      'default',
      exit_condition_lambda=kube_utils.pod_is_done,
      condition_description='done state',
      exponential_backoff=True)

  if resp.status.phase == kube_utils.PodPhase.FAILED.value:
    raise RuntimeError('Pod "default:%s" failed with status "%s".' %
                       (pod_name, resp.status))


def _serialize_pipeline(pipeline: tfx_pipeline.Pipeline) -> Text:
  """Serializes a TFX pipeline.

  To be replaced with the the TFX Intermediate Representation:
  tensorflow/community#271. This serialization procedure extracts from
  the pipeline properties necessary for reconstructing the pipeline instance
  from within the cluster. For properties such as components and metadata
  config that can not be directly dumped with JSON, we use NodeWrapper and
  MessageToJson to serialize them beforehand.

  Args:
    pipeline: Logical pipeline containing pipeline args and components.

  Returns:
    Pipeline serialized as JSON string.
  """
  serialized_components = []
  for component in pipeline.components:
    serialized_components.append(
        json_utils.dumps(node_wrapper.NodeWrapper(component)))
  # Extract and pass pipeline graph information which are lost during the
  # serialization process. The orchestrator container uses downstream_ids
  # to reconstruct pipeline graph.
  downstream_ids = _extract_downstream_ids(pipeline.components)
  return json.dumps({
      'pipeline_name': pipeline.pipeline_info.pipeline_name,
      'pipeline_root': pipeline.pipeline_info.pipeline_root,
      'enable_cache': pipeline.enable_cache,
      'components': serialized_components,
      'downstream_ids': downstream_ids,
      'metadata_connection_config': json_format.MessageToJson(
          message=pipeline.metadata_connection_config,
          preserving_proto_field_name=True,
      ),
      'beam_pipeline_args': pipeline.beam_pipeline_args,
  })


def _extract_downstream_ids(
    components: List[base_node.BaseNode]) -> Dict[Text, List[Text]]:
  """Extract downstream component ids from a list of components.

  Args:
    components: List of TFX Components.

  Returns:
    Mapping from component id to ids of its downstream components for
    each component.
  """

  downstream_ids = {}
  for component in components:
    downstream_ids[component.id] = [
        downstream_node.id for downstream_node in component.downstream_nodes]
  return downstream_ids
