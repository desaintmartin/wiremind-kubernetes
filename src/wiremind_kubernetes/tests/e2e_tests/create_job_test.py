import logging
import time

import kubernetes

from .conftest import TEST_NAMESPACE

logger = logging.getLogger(__name__)


def test_create_job(concerned_dm, create_namespace):
    """
    Test that default create job and delete job work as expected
    """
    job_name = "my-test-job"
    concerned_dm.create_job(
        concerned_dm.generate_job(
            job_name=job_name, container_image="gcr.io/google_containers/pause-amd64:3.1", labels={"foo": "bar"}
        )
    )
    for _ in range(1, 20):
        created_job = concerned_dm.client_batchv1_api.read_namespaced_job(
            concerned_dm.release_name + "-" + job_name, TEST_NAMESPACE
        )
        if created_job.status.active == 1:
            break
        else:
            logger.info("job not ready yet, waiting...")
            time.sleep(5)
    assert created_job.status.active == 1

    concerned_dm.delete_job(job_name=job_name)
    for _ in range(1, 10):
        try:
            concerned_dm.client_batchv1_api.read_namespaced_job(
                concerned_dm.release_name + "-" + job_name, TEST_NAMESPACE
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                break
        else:
            logger.info("job is not deleted yet, waiting...")
            time.sleep(1)

    for _ in range(1, 10):
        pod_list = concerned_dm.client_corev1_api.list_namespaced_pod(
            TEST_NAMESPACE, label_selector=f"job-name={concerned_dm.release_name}-{job_name}"
        ).items
        if not pod_list:
            break
        else:
            logger.info("Related pod is not deleted yet, waiting...")
            time.sleep(1)
    assert not pod_list


def test_create_job_argument(concerned_dm, create_namespace):
    """
    Test that create job with command / args works and finishes as expected
    """
    job_name = "my-test-job"
    concerned_dm.create_job(
        concerned_dm.generate_job(
            job_name=job_name, container_image="alpine:latest", command="sh", args=["-c", "true"], labels={"foo": "bar"}
        )
    )

    for _ in range(1, 20):
        created_job = concerned_dm.client_batchv1_api.read_namespaced_job(
            concerned_dm.release_name + "-" + job_name, TEST_NAMESPACE
        )
        if created_job.status.succeeded == 1:
            break
        else:
            logger.info("job not finished yet, waiting...")
            time.sleep(5)
    assert created_job.status.succeeded == 1
