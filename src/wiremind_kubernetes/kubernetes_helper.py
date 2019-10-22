# -*- coding: utf-8 -*-
import logging
import os
import time

import kubernetes
from future.standard_library import install_aliases

from wiremind_kubernetes.exceptions import PodNotFound

from .kube_config import load_kubernetes_config
from .utils import retry_kubernetes_request

install_aliases()


logger = logging.getLogger(__name__)


class KubernetesHelper(object):
    """
    A simple helper for Kubernetes manipulation.
    """

    deployment_namespace = None
    client_appsv1_api = None
    client_custom_objects_api = None

    def __init__(self, use_kubeconfig=False, deployment_namespace=None, dry_run=False):
        """
        :param use_kubeconfig:
            Use ~/.kube/config file to authenticate.
            If false, use kubernetes built-in in_cluster mechanism.
            Defaults to False.
        :param deployment_namespace:
            Target namespace to use.
            If not defined, try to get it from kubernetes built-in serviceAccount mechanism.
        :param dry_run:
            Dry run.
        """
        load_kubernetes_config(use_kubeconfig=use_kubeconfig)
        self.client_appsv1_api = kubernetes.client.AppsV1Api()
        self.core_api = kubernetes.client.CoreV1Api()
        self.client_custom_objects_api = kubernetes.client.CustomObjectsApi()

        self.dry_run = dry_run

        self.read_additional_arguments = dict(pretty=True)
        self.additional_arguments = self.read_additional_arguments.copy()
        if dry_run:
            self.additional_arguments['dry_run'] = 'All'

        if deployment_namespace:
            self.deployment_namespace = deployment_namespace
        else:
            self.deployment_namespace = open(
                "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
            ).read()

    @retry_kubernetes_request
    def get_deployment_scale(self, deployment_name):
        logger.debug("Getting deployment scale for %s", deployment_name)
        return self.client_appsv1_api.read_namespaced_deployment_scale(
            deployment_name, self.deployment_namespace, **self.read_additional_arguments
        )

    @retry_kubernetes_request
    def get_statefulset_scale(self, statefulset_name):
        logger.debug("Getting statefulset scale for %s", statefulset_name)
        return self.client_appsv1_api.read_namespaced_stateful_set_scale(
            statefulset_name, self.deployment_namespace, **self.read_additional_arguments
        )

    def scale_down_statefulset(self, statefulset_name):
        body = self.get_statefulset_scale(statefulset_name)
        logger.debug("Deleting all Pods for %s", statefulset_name)
        body.spec.replicas = 0
        self.client_appsv1_api.patch_namespaced_stateful_set_scale(
            statefulset_name, self.deployment_namespace, body, **self.additional_arguments
        )
        logger.debug("Done deleting.")

    @retry_kubernetes_request
    def scale_down_deployment(self, deployment_name):
        body = self.get_deployment_scale(deployment_name)
        logger.debug("Deleting all Pods for %s", deployment_name)
        body.spec.replicas = 0
        self.client_appsv1_api.patch_namespaced_deployment_scale(
            deployment_name, self.deployment_namespace, body, **self.additional_arguments
        )
        logger.debug("Done deleting.")

    def scale_up_statefulset(self, statefulset_name, pod_amount=1):
        body = self.get_statefulset_scale(statefulset_name)
        logger.debug("Recreating backend Pods for %s", statefulset_name)
        body.spec.replicas = pod_amount
        self.client_appsv1_api.patch_namespaced_stateful_set_scale(
            statefulset_name, self.deployment_namespace, body, **self.additional_arguments
        )
        logger.debug("Done recreating.")

    @retry_kubernetes_request
    def scale_up_deployment(self, deployment_name, pod_amount):
        body = self.get_deployment_scale(deployment_name)
        logger.debug("Recreating backend Pods for %s", deployment_name)
        body.spec.replicas = pod_amount
        self.client_appsv1_api.patch_namespaced_deployment_scale(
            deployment_name, self.deployment_namespace, body, **self.additional_arguments
        )
        logger.debug("Done recreating.")

    def is_statefulset_stopped(self, deployment_name):
        return self.is_deployment_stopped(deployment_name, statefulset=True)

    @retry_kubernetes_request
    def is_deployment_stopped(self, deployment_name, statefulset=False):
        logger.debug("Asking if deployment %s is stopped", deployment_name)
        if statefulset:
            scale = self.client_appsv1_api.read_namespaced_stateful_set_scale(
                deployment_name, self.deployment_namespace, **self.read_additional_arguments
            )
        else:
            scale = self.client_appsv1_api.read_namespaced_deployment_scale(
                deployment_name, self.deployment_namespace, **self.read_additional_arguments
            )
        logger.debug('%s has %s replicas', deployment_name, scale.status.replicas)
        return scale.status.replicas == 0 or self.dry_run

    def is_statefulset_ready(self, statefulset_name):
        statefulset_status = self.client_appsv1_api.read_namespaced_stateful_set_status(
            statefulset_name, self.deployment_namespace, **self.read_additional_arguments
        )
        expected_replicas = statefulset_status.spec.replicas
        ready_replicas = statefulset_status.status.ready_replicas
        logger.debug(
            'StatefulSet %s has %s expected replicas and %s ready replicas',
            statefulset_name, expected_replicas, ready_replicas
        )
        return expected_replicas == ready_replicas

    @retry_kubernetes_request
    def getPodNameFromDeployment(self, deployment_name, namespace_name):
        """
        From a given deployment, get the first pod name
        """
        try:
            deployment = self.client_appsv1_api.read_namespaced_deployment(deployment_name, namespace_name)
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                raise PodNotFound(
                    'No deployment %s was found in the namespace %s' % (deployment_name, namespace_name)
                )
            else:
                raise
        selector = ','.join('%s=%s' % (key, value) for key, value in deployment.spec.selector.match_labels.items())
        pod_list = self.core_api.list_namespaced_pod(namespace_name, label_selector=selector).items
        if not pod_list:
            raise PodNotFound(
                'No matching pod was found in the namespace %s' % (namespace_name)
            )
        return pod_list[0].metadata.name


class KubernetesDeploymentManager(KubernetesHelper):
    """
    Subclass of Kubernetes Helper allowing to scale down/up all pods that
    should be stopped/started when doing database migration/maintenance
    (alembic, dump, etc).
    The associated Deployment should define an eds.

    Usage:
    a = wiremind_kubernetes.KubernetesDeploymentManager(use_kubeconfig=True, deployment_namespace="my-namespace")
    a.stop_pods()
    do_something('wololo')
    a.start_pods()
    """

    def __init__(self, release_name=None, **kwargs):
        if release_name:
            self.release_name = release_name
        else:
            self.release_name = os.environ.get('RELEASE_NAME')
        super(KubernetesDeploymentManager, self).__init__(**kwargs)

    def start_pods(self):
        """
        Start all Pods that should be started
        """
        expected_deployment_scale_dict = self._get_expected_deployment_scale_dict()

        if not expected_deployment_scale_dict:
            return

        logger.info("Scaling up application Deployments...")
        for (name, amount) in expected_deployment_scale_dict.items():
            self.scale_up_deployment(name, amount)
        logger.info("Done scaling up application Deployments.")

    def stop_pods(self):
        """
        SQL migration implies that every backend pod should be restarted.
        We stop them before applying migration
        """
        expected_deployment_scale_dict = self._get_expected_deployment_scale_dict()

        if not expected_deployment_scale_dict:
            return

        logger.info("Scaling down application Deployments...")
        for deployment_name in expected_deployment_scale_dict.keys():
            self.scale_down_deployment(deployment_name)

        # Make sure to wait for actual stop (can be looong)
        for _ in range(360):  # 1 hour
            time.sleep(1)
            stopped = 0
            for deployment_name in expected_deployment_scale_dict.keys():
                if self.is_deployment_stopped(deployment_name):
                    stopped += 1
            if stopped == len(expected_deployment_scale_dict):
                break
            else:
                logger.info("All pods not deleted yet. Waiting...")
        logger.info("Done scaling down application Deployments, all Pods have been deleted.")

    @retry_kubernetes_request
    def _get_expected_deployment_scale_dict(self):
        """
        Return a dict of expected deployment scale

        key: Deployment name, only if it has an associated eds
        value: expected Deployment Scale (replicas)
        """
        logger.debug("Getting Expected Deployment Scale list")
        if not self.release_name:
            eds_list = self.client_custom_objects_api.list_namespaced_custom_object(
                namespace=self.deployment_namespace,
                group="wiremind.fr",
                version="v1",
                plural="expecteddeploymentscales"
            )
        else:
            eds_list = self.client_custom_objects_api.list_namespaced_custom_object(
                namespace=self.deployment_namespace,
                group="wiremind.fr",
                version="v1",
                plural="expecteddeploymentscales",
                label_selector="release=%s" % self.release_name,
            )
        eds_dict = {
            eds['spec']['deploymentName']: eds['spec']['expectedScale'] for eds in eds_list['items']
        }
        logger.debug("List is %s", eds_dict)
        return eds_dict
