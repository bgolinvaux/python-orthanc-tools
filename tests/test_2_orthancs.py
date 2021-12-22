import time
import sys
import pprint
import unittest
import subprocess
import tempfile
from threading import Timer
import shutil
import logging
from orthanc_api_client import OrthancApiClient, ChangeType
from orthanc_api_client.helpers import wait_until
import orthanc_api_client.exceptions as api_exceptions
import pathlib
import os
import logging

from orthanc_tools import OrthancCloner, OrthancMonitor

here = pathlib.Path(__file__).parent.resolve()

logger = logging.getLogger('orthanc_tools')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)


class Test2Orthancs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        subprocess.run(["docker-compose", "down", "-v"], cwd=here/"docker-setup")
        subprocess.run(["docker-compose", "up", "-d"], cwd=here/"docker-setup")

        cls.oa = OrthancApiClient('http://localhost:10042', user='test', pwd='test')
        cls.oa.wait_started()
        cls.ob = OrthancApiClient('http://localhost:10043', user='test', pwd='test')
        cls.ob.wait_started()

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker-compose", "down", "-v"], cwd=here/"docker-setup")

    def test_cloner(self):
        self.oa.delete_all_content()
        self.ob.delete_all_content()

        self.oa.upload_file(here / "stimuli/CT_small.dcm")

        cloner = OrthancCloner(source=self.oa, destination=self.ob)
        cloner.execute()

        self.assertEqual(len(self.oa.instances.get_all_ids()), len(self.ob.instances.get_all_ids()))

    def test_monitor(self):
        processed_instances = []

        monitor = OrthancMonitor(
            self.oa,
            polling_interval=0.1
        )
        monitor.add_handler(ChangeType.NEW_INSTANCE, lambda instance_id, api_client: processed_instances.append(instance_id))

        monitor.start()

        self.oa.upload_file(here / "stimuli/CT_small.dcm")

        wait_until(lambda: len(processed_instances) > 0, 30)

        monitor.stop()
        self.assertEqual(1, len(processed_instances))

    def test_monitor_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            persist_status_path = os.path.join(temp_dir, 'seq.txt')

            processed_resources = []

            def process_instance(instance_id, api_client):
                logger.info(f'processing instance {instance_id}')
                time.sleep(5)
                processed_resources.append(instance_id)
                return True

            def process_series(series_id, api_client):
                logger.info(f'processing series {series_id}')
                processed_resources.append(series_id)
                return True

            monitor = OrthancMonitor(
                self.oa,
                polling_interval=0.1,
                persist_status_path=persist_status_path,
                workers_count=4
            )

            # first event is lengthy (5 seconds) and will not be processed at the time we first check the sequence id file
            # monitor.add_handler(ChangeType.NEW_INSTANCE, lambda instance_id, api_client: Timer(5, lambda: processed_resources.append(instance_id)).start())
            monitor.add_handler(ChangeType.NEW_INSTANCE, process_instance)
            monitor.add_handler(ChangeType.NEW_SERIES, process_series)
            # monitor.add_handler(ChangeType.NEW_SERIES, lambda series_id, api_client: processed_resources.append(series_id))

            self.oa.upload_file(here / "stimuli/CT_small.dcm")

            monitor.start()
            wait_until(lambda: len(processed_resources) > 0, 1)
            with open(persist_status_path, "rt") as f:
                seq_id = int(f.read())

            all_changes, last_seq_id, done = self.oa.get_changes()
            all_series_ids = self.oa.series.get_all_ids()
            all_instances_ids = self.oa.instances.get_all_ids()

            self.assertGreaterEqual(all_changes[0].sequence_id, seq_id)        # change '1' has not been processed yet
            self.assertEqual(all_series_ids[0], processed_resources[0])     # change '2' has been processed
            self.assertEqual(1, len(processed_resources))  # the instance has not been processed yet because of the sleep 5

            monitor.stop()

            wait_until(lambda: len(processed_resources) == 2, 6)
            self.assertEqual(2, len(processed_resources))  # the instance should have been processed by now

            with open(persist_status_path, "rt") as f:
                seq_id = int(f.read())

            self.assertLessEqual(all_changes[-1].sequence_id, seq_id)        # all changes have been processed



if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    unittest.main()

