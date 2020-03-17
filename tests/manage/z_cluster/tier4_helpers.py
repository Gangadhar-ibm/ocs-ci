import logging
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from ocs_ci.ocs import constants
from ocs_ci.ocs import defaults
from ocs_ci.ocs.ocp import OCP
import ocs_ci.ocs.resources.pod as pod_helpers

logger = logging.getLogger(__name__)


def raw_block_io(raw_blk_pod,size='100G'):
    """
    Runs the block ios on pod baased raw block pvc
    Args:
        raw_blk_pod(pod): pod on which  block IOs should run
        size(str): IO size

    Returns:

    """
    raw_blk_pod.run_io(storage_type = 'block', size = size)
    return True


# move this function to cluster.py
def get_osd_size():
    """
    Function to get the osd size in a cluster

    Returns:
        int: The size of the osd

    """
    ocp = OCP(namespace=defaults.ROOK_CLUSTER_NAMESPACE, kind=constants.STORAGECLUSTER)
    sc = ocp.get()

    # the following series of 'get's return capacity with size qualifier like Gi/Ti attached. So, take out last two
    # characters to get actual capacity in int.
    return sc.get('items')[0].get('spec').get('storageDeviceSets')[0].get('dataPVCTemplate').\
        get('spec').get('resources').get('requests').get('storage')[:-2]


def get_percent_used_capacity():
    """
    Function to calculate the percentage of used capacity in a cluster

    Returns:
        int: The percentage of the used capacity in the cluster
    """
    ct_pod = pod_helpers.get_ceph_tools_pod()
    output = ct_pod.exec_ceph_cmd(ceph_cmd='rados df')
    return output.get('total_used') / output.get('total_space') * 100


def downloader(dest_pod):
    """
     Downloads a wellknown file from the net on a given pod's /mnt directory

    Args:
        dest_pod: pod on which the file need to be downloaded

    Returns:

    """
    curl_cmd = "" + "if [ -e /mnt/ceph.tar.gz ]; " \
                    "then " \
                    "   echo /mnt/ceph.tar.gz exists.; " \
                    "else " \
                    "   curl http://download.ceph.com/tarballs/ceph_15.1.0.orig.tar.gz --output /mnt/ceph.tar.gz ; " \
                    "fi" \
               + ""
    dest_pod.exec_sh_cmd_on_pod(curl_cmd, sh="bash")
    logging.info(f"#### downloaded ceph.tar.gz file in {dest_pod.name}")


def filler(fill_pod):
    """
    This function copies the file downloaded by 'downloader' function in a unique directory to increase the
    cluster space utilization. Currently it makes 30 copies of the downloaded file in a given directory which is
     equivalent to almost 4 GiB of storage.

    Args:
        fill_pod: the pod on which the storage space need to be filled.

    Returns:

    """
    target_dir_name = "/mnt/cluster_fillup0_" + uuid4().hex
    mkdir_cmd = "" + "mkdir " + target_dir_name + ""
    logging.info("#################### JUNK 1 ######################3")
    fill_pod.exec_sh_cmd_on_pod(mkdir_cmd, sh="bash")

    tee_cmd = "" + " tee " + target_dir_name + "/ceph.tar.gz{1..30} < /mnt/ceph.tar.gz >/dev/null" + ""
    #tee_cmd = "" + " tee " + target_dir_name + "/ceph.tar.gz{1..30} < /mnt/ceph.tar.gz >/dev/null &" + ""
    logging.info("#################### JUNK 2 ######################3")
    fill_pod.exec_sh_cmd_on_pod(tee_cmd, sh="bash")
    logging.info(f"Executed command {tee_cmd}")


def cluster_filler(pods_to_fill, percent_required_filled):
    """
    This function does the following:
        1) calls 'downloader' to download a file from the wellknown location in the net
        2) calls 'filler' to copy the above downloaded file into an unique directory on the given pod if the
        %age of the space used in cluster is lesser than the 'percent_required_filled'.

    Args:
        pods_to_fill(list): List of pods on which the file needs to be copied to increase the cluster space utilized
        percent_required_filled(int): The percentage to which the cluster has its storage utilized

    Returns:

    """
    with ThreadPoolExecutor() as downloader_executor:
        for p in pods_to_fill:
            downloader_executor.submit(downloader, p)
            logging.info(f"### initiated downloader for {p.name}")
    concurrent_copies = 5 # 3
    cluster_filled = False

    filler_executor = ThreadPoolExecutor()
    while not cluster_filled:
        for copy_iter in range(concurrent_copies):
            for each_pod in pods_to_fill:
                used_capacity = get_percent_used_capacity()
                logging.info(f"### used capacity %age = {used_capacity}")
                if used_capacity <= percent_required_filled:
                    filler_executor.submit(filler, each_pod)
                    logging.info(f"#### Ran copy operation on pod {each_pod.name}. copy_iter # {copy_iter}")
                else:
                    logging.info(f"############ Cluster filled to the expected capacity {percent_required_filled}")
                    cluster_filled = True
                    filler_executor.shutdown(wait=False)
                    break
            if cluster_filled:
                break


def cluster_copy_ops(copy_pod):
    """
    Function to do copy operations in a given pod. Mainly used as a background IO during cluster expansion.
    It does of series of copy operations and verifies the data integrity of the files copied.

    Args:
        copy_pod(pod): on which copy operations need to be done

    Returns:
        Boolean: False, if there is data integrity check failure. Else, True

    """

    dir_name = "cluster_copy_ops_" + uuid4().hex
    cmd = "" + "mkdir /mnt/"+ dir_name + ""
    copy_pod.exec_sh_cmd_on_pod(cmd, sh="bash")

    # cp ceph.tar.gz 10 times to each cpdir
    cmd = "" + "mkdir /mnt/"+ dir_name + "/copy_dir{1..10} ; " \
               "for i in {1..10}; do cp /mnt/ceph.tar.gz /mnt/"+ dir_name + "/copy_dir$i/ceph_$i.tar.gz & done"\
          + ""
    copy_pod.exec_sh_cmd_on_pod(cmd, sh="bash")

    # check md5sum
    # since the file to be copied is from a wellknown location, we calculated its md5sum and found it to be:
    # 016c37aa72f12e88127239467ff4962b. We will pass this value to the pod to see if it matches that of the same
    # file copied in different directories of the pod.
    # (we could calculate the md5sum by downloading first outside of pod and then comparing it with that of pod.
    # But this will increase the execution time as we have to wait for download to complete once outside pod and
    # once inside pod)
    md5sum_val_expected = "016c37aa72f12e88127239467ff4962b"

    # We are not using the pod.verify_data_integrity with the fedora dc pods as of now for the following reason:
    # verify_data_integrity function in pod.py calls check_file_existence which in turn uses 'find' utility to see
    # if the file given exists or not. In fedora pods which this function mainly deals with, the 'find' utility
    # doesn't come by default. It has to be installed. While doing so, 'yum install findutils' hangs.
    # the link "https://forums.fedoraforum.org/showthread.php?320926-failovemethod-option" mentions the solution:
    # to run "sed -i '/^failovermethod=/d' /etc/yum.repos.d/*.repo". This command takes at least 6-7 minutes to
    # complete. If this has to be repeated on 24 pods, then time taken to complete may vary between 20-30 minutes
    # even if they are run in parallel threads.
    # Instead of this if we use shell command: "md5sun" directly we can reduce the time drastically. And hence we
    # are not using verify_data_integrity() here.

    for i in range(1, 10):

        cmd = "" + "md5sum /mnt/"+ dir_name + "/copy_dir" + str(i) +"/ceph_" + str(i) + ".tar.gz " + ""
        output = copy_pod.exec_sh_cmd_on_pod(cmd, sh="bash")
        md5sum_val_got = output.split("  ")[0]
        logger.info(f"#### md5sum obtained for pod: {copy_pod.name} is {md5sum_val_got}")
        logger.info(f"#### Expected was: {md5sum_val_expected}")
        if md5sum_val_got != md5sum_val_expected:
            logging.info(f"***** md5sum check FAILED. expected: {md5sum_val_expected}, but got {md5sum_val_got}")
            return False

    logging.info("#### Data Integrity check passed")

    # Remove the directories - clean up
    cmd = "" + "rm -rf /mnt/" + dir_name + "/copy_dir{1..10}" + ""
    logging.info(f"#### command to remove = {cmd}")
    copy_pod.exec_sh_cmd_on_pod(cmd, sh="bash")

    return True
