import argparse
import datetime
import logging
import time
import uuid
import os
import threading
from strenum import StrEnum
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from enum import Enum

from orthanc_api_client import OrthancApiClient, Study, Series, JobStatus, ResourceNotFound, InstancesSet, ResourceType
from .helpers.scheduler import Scheduler
from .helpers.time_out import TimeOut
from .orthanc_monitor import OrthancMonitor, ChangeType

logger = logging.getLogger('orthanc_tools')


class ForwarderMode(StrEnum):
    DICOM = 'dicom'             # use DICOM
    DICOM_SERIES_BY_SERIES = 'dicom-series-by-series'             # use DICOM but create a new association for each series
    DICOM_WEB = 'dicom-web'     # use DicomWEB
    DICOM_WEB_SERIES_BY_SERIES = 'dicom-web-series-by-series'     # use DicomWEB but one request per series to avoid large payloads.  This will also split very large series into < 1GB requests
    PEERING = 'peering'         # use peering between 2 orthancs
    TRANSFER = 'transfer'       # use the transfer plugin accelerator between 2 orthancs


@dataclass
class ForwarderDestination:
    destination: str                        # the alias of the destination Modality, Peer or DicomWeb server
    forwarder_mode: ForwarderMode           # the mode to use to forward to the destination
    alternate_destination: str = None       # an alternate destination in case this one can not be contacted


# class ForwarderMetadata(Enum):
#     INSTANCE_PROCESSED = 4600
#     SENT_TO_DESTINATIONS = 4601
#     NEXT_RETRY = 4602


@dataclass
class ForwarderInstancesSetStatus:
    processed: bool = field(init=False, default=False)
    sent_to_destinations: List[str] = field(default_factory=list)
    retry_count: int = field(init=False, default=0)
    next_retry: Optional[datetime.datetime] = None


class OrthancForwarder:
    """
    Forwards everything Orthanc receives to another Orthanc peer, a DICOM modality or DicomWeb server.
    The Forwarder deletes the study/instances once they have been forwarded.

    The images may be modified before being sent.  In that case, you should:
    - either provide an instance_processor callback if you are modifying the instances 'in_place' (keeping the same Orthanc ids)
    - or override process() in a subclass
    The modifications shall be idempotent:  it shall always give the same result if you repeat the modification multiple times

    The images may be filtered out before being processed and forwarded.  In that case, you should:
    - either provide an instance_filter callback
    - or override filter() in a subclass
    Images that are filtered out are deleted from the forwarder.

    To run correctly, an OrthancForwarder must have the UserMetadata 'ModifiedByOrthancForwarder' defined in its config file.

    An OrthancForwarder may be triggered by two 'events': the stable study or the 'instance received' event.

    You might define multiple destinations -> the Forwarder will send the study to all destinations and delete the study only once the study has been sent to all destinations.
    i.e: destinations = [Destination(A, PEER), Destination(B, DICOM)]
    -> it will send to A and B

    You might also define alternate destinations that will be used when the primary destination is unreachable.
    i.e: destinations = [Destination(A, PEER, alternateDestination = Destination(B, DICOM))]
    -> it will try to send to A and, if A is down, will send to B

    If the forwarding fails, the Forwarder will retry to send the instances later on.

    The OrthancForwarder uses Orthanc metadata ranging between [4600, 4700[
    """

    retry_intervals = [60, 120, 300, 1800, 3600]

    def __init__(self,
                 source: OrthancApiClient,
                 destinations: List[ForwarderDestination],
                 trigger: ChangeType = ChangeType.STABLE_STUDY,
                 max_retry_count_at_startup: int = 5,
                 polling_interval_in_seconds: int = 1,
                 worker_threads_count: int = 1,
                 instance_filter = None,    # a method to filter instances.  Signature: Filter(api_client, instance_id) -> bool
                 instance_processor = None  # a method to process instances before forwarding them.  Signature: Process(api_client, instance_id)
                 ):

        self._source = source
        self._destinations = destinations
        self._trigger = trigger
        self._max_retry_count_at_startup = max_retry_count_at_startup
        self._polling_interval_in_seconds = polling_interval_in_seconds
        self._is_running = False
        self._worker_threads_count = worker_threads_count
        self._execution_thread = None
        self._instance_filter = instance_filter
        self._instance_processor = instance_processor
        self._status = {}

    def wait_orthanc_started(self):
        retry = 0
        while not self._source.is_alive():
            logger.info("Waiting to connect to Orthanc")
            retry += 1
            if retry == self._max_retry_count_at_startup:
                logger.error("Could not connect to Orthanc at startup")
                raise Exception("Could not connect to Orthanc at startup")
            time.sleep(self._polling_interval_in_seconds)

        system = self._source.get_system()
        if "OverwriteInstances" not in system:
            logger.warning("Unable to check OverwriteInstances configuration")
        elif not system["OverwriteInstances"]:
            if self._instance_processor:
                logger.error("Orthanc Forwarder: when providing an instance_processor, you should have OverwriteInstances set to true to replace the instance with the new one")
                raise Exception("Invalid Orthanc configuration: OverwriteInstances is false")

        # TODO: find a way to check that ModifiedByOrthancForwarder metadata is defined

    def execute(self):  # runs forever !
        self.wait_orthanc_started()

        while True:
            self._execute_once()
            time.sleep(self._polling_interval_in_seconds)

    def _thread_execute(self):
        while self._is_running:
            self._execute_once()
            time.sleep(self._polling_interval_in_seconds)

    def _execute_once(self):

        # Everytime we run the forwarder, we run an OrthancMonitor that tries to process all existing changes
        # Therefore, failures will be reprocessed the next time
        monitor = OrthancMonitor(api_client=self._source,
                                 worker_threads_count=self._worker_threads_count,
                                 max_retries=0,  # no retries, we will handle retries from here
                                 polling_interval=0.1
                                 )

        if self._trigger == ChangeType.STABLE_STUDY:
            monitor.add_handler(change_type=self._trigger, callback=self._handle_study)
        elif self._trigger == ChangeType.STABLE_SERIES:
            monitor.add_handler(change_type=self._trigger, callback=self._handle_series)
        elif self._trigger == ChangeType.NEW_INSTANCE:
            monitor.add_handler(change_type=self._trigger, callback=self._handle_instance)

        monitor.execute(existing_changes_only=True)

    def start(self):
        logger.info("Starting Orthanc Forwarder")

        # create execution thread
        self._execution_thread = threading.Thread(
            target = self._thread_execute,
            name = 'OrthancForwarder execution thread'
        )

        self.wait_orthanc_started()

        # start threads
        self._is_running = True
        self._execution_thread.start()

    def stop(self):
        logger.info("Stopping Orthanc Forwarder")

        self._is_running = False
        self._execution_thread.join()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def _handle_study(self, change_id: int, study_id: str, api_client):
        instances_set = InstancesSet.from_study(api_client=api_client, study_id=study_id)
        self.handle_instances_set(instances_set)

    def _handle_series(self, change_id: int, series_id: str, api_client):
        instances_set = InstancesSet.from_series(api_client=api_client, series_id=series_id)
        self.handle_instances_set(instances_set)

    def _handle_instance(self, change_id: int, instance_id: str, api_client):
        instances_set = InstancesSet.from_instance(api_client=api_client, instance_id=instance_id)
        self.handle_instances_set(instances_set)

    def filter(self, instances_set: InstancesSet) -> InstancesSet:
        # this method can be overriden in a derived class.
        # By default, all instances not satisfying the filter are deleted
        if self._instance_filter:
            filtered = instances_set.filter_instances(self._instance_filter)
            logger.info(f"{instances_set} Deleting {len(filtered.instances_ids)} instances / {len(filtered.series_ids)} series that have been filtered out")
            filtered.delete()

        return instances_set

    def process(self, instances_set: InstancesSet) -> bool:
        # this method can be overriden in a derived class.

        if self._instance_processor:
            try:
                logger.info(f"{instances_set} Processing ...")

                instances_set.process_instances(self._instance_processor)

                logger.info(f"{instances_set} Processing ... done")
            except Exception as ex:
                logger.error(f"{instances_set} Error while processing: {ex}", exc_info=1)
                return False

        return True

    def forward(self, instances_set, already_sent_to_destinations: List[str]) -> List[str]:  # returns a list of destinations where the data has been sent
        sent_to_destinations = []

        # has_been_sent_to = self._status[instances_set.id].sent_to_destinations
        # check the metadata of a random instance to detect to which destinations it has already been sent (which would mean that we are retrying to process the set)
        #has_been_sent_to = self._source.instances.get_string_metadata(instances_set.instances_ids[0], metadata_name=str(ForwarderMetadata.SENT_TO_DESTINATIONS.value), default_value="").split(",")

        for dest in self._destinations:
            try:

                if dest.destination not in already_sent_to_destinations:
                    logger.info(f"{instances_set} Sending to {dest.destination} using {dest.forwarder_mode}")
                    self._forward_to_destination(
                        instances_set=instances_set,
                        destination=dest
                    )
                    logger.info(f"{instances_set} Sent")
                else:
                    logger.info(f"{instances_set} Sending ... already sent to {dest.destination} using {dest.forwarder_mode}")

                sent_to_destinations.append(dest.destination)
            except Exception as ex:
                logger.error(f"{instances_set} Error while forwarding to {dest.destination}: {ex}", exc_info=1)

        return sent_to_destinations
            # has_been_sent_to = self._source.instances.get_string_metadata(instances_set.instances_ids[0], metadata_name=str(ForwarderMetadata.SENT_TO_DESTINATIONS.value), default_value="").split(",")


        # only save the sent_to_destinations if there are multiple destinations and there has been a failure.  Otherwise, we'll delete the data anyway right after
        # if len(self._destinations) > 1 and len(sent_to_destinations) > 1:
        #     self._set_string_metadata(instances_set, metadata_name=str(ForwarderMetadata.SENT_TO_DESTINATIONS.value), content=",".join(sent_to_destinations))
        # self._status[instances_set.id].sent_to_destinations = sent_to_destinations


    def delete(self, instances_set):
        logger.info(f"{instances_set} Deleting ...")
        instances_set.delete()
        logger.info(f"{instances_set} Deleting ... Done")

    def handle_instances_set(self, instances_set: InstancesSet):

        if instances_set.id not in self._status:
            self._status[instances_set.id] = ForwarderInstancesSetStatus()
        else:  # this is a retry !
            if datetime.datetime.now < self._status[instances_set.id].next_retry:
                logger.debug(f"{instances_set} Skipping while waiting for retry")
                return

        logger.info(f"{instances_set} Handling ...")

        # filter
        instances_set = self.filter(instances_set)

        # process
        if not self._status[instances_set.id].processed:
            self._status[instances_set.id].processed = self.process(instances_set)
        else:
            logger.info(f"{instances_set} Skipping processing that has already been performed")

        # forward
        sent_to_destinations = self.forward(instances_set, self._status[instances_set.id].sent_to_destinations)
        if len(sent_to_destinations) == len(self._destinations):
            # delete
            self.delete(instances_set)
        else:
            self._status[instances_set.id].sent_to_destinations = sent_to_destinations

            retry_count = self._status[instances_set.id].retry_count
            next_retry = datetime.datetime.now() + datetime.timedelta(seconds=self.retry_intervals[min(retry_count, len(self.retry_intervals) - 1)])
            logger.debug(f"{instances_set} Failed, will retry at {next_retry}")

            self._status[instances_set.id].next_retry = next_retry
            self._status[instances_set.id].retry_count = retry_count + 1
            return

        logger.info(f"{instances_set} Handling ... Done")

    def _forward_to_destination(self, instances_set: InstancesSet, destination: ForwarderDestination):
        if destination.forwarder_mode == ForwarderMode.DICOM:
            self._source.modalities.send(
                target_modality=destination.destination,
                resources_ids=instances_set.instances_ids
            )
        elif destination.forwarder_mode == ForwarderMode.DICOM_SERIES_BY_SERIES:
            for s in instances_set.series_ids:
                self._source.modalities.send(
                    target_modality=destination.destination,
                    resources_ids=instances_set.get_instances_ids(series_id=s),
                    synchronous=True
                )
        elif destination.forwarder_mode == ForwarderMode.DICOM_WEB:
            for s in instances_set.series_ids:
                self._source.dicomweb_servers.send(
                    target_server=destination.destination,
                    resources_ids=instances_set.get_instances_ids(series_id=s)
                )
        elif destination.forwarder_mode == ForwarderMode.DICOM_WEB_SERIES_BY_SERIES:
            for s in instances_set.series_ids:
                series = self._source.series.get(s)
                if series.statistics.uncompressed_size > 1*1024*1024*1024:
                    logger.info(f"{instances_set} A series is larger than 1 GB, sending instance by instance")
                    for i in instances_set.get_instances_ids(seris_id=s):
                        self._source.dicomweb_servers.send(
                            target_server=destination.destination,
                            resources_ids=[i]
                        )
                else:
                    self._source.dicomweb_servers.send(
                        target_server=destination.destination,
                        resources_ids=instances_set.get_instances_ids(series_id=s)
                    )
        elif destination.forwarder_mode == ForwarderMode.PEERING:
            self._source.peers.send(
                target_peer=destination.destination,
                resources_ids=instances_set.instances_ids
            )

        elif destination.forwarder_mode == ForwarderMode.TRANSFER:
            self._source.transfers.send(
                target_peer=destination.destination,
                resources_ids=instances_set.instances_ids,
                resource_type=ResourceType.INSTANCE
            )

        else:
            raise NotImplementedError

    def _set_string_metadata(self, instances_set: InstancesSet, metadata_name: str, content: str):
            instances_set.process_instances(lambda c, i: c.instances.set_string_metadata(
                orthanc_id=i,
                metadata_name=metadata_name,
                content=content
            ))