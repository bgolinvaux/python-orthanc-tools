import unittest, os, glob
import hl7  # https://python-hl7.readthedocs.org/en/latest/
import pydicom
from orthanc_tools import MLLPClient, DicomWorklistBuilder, Hl7WorklistParser, Hl7MessageValidator, MLLPServer, Hl7OrmWorklistMsgHandler
import tempfile
import logging


class TestHl7OrmWorklistMsgHandler(unittest.TestCase):

    def test_avignon_with_ge_modality(self):
        port_number = 2002  # there are currently some issues when trying to reuse the same port in 2 tests (it's probably not freed soon enough -> let's use another port for each test)
        with tempfile.TemporaryDirectory() as temporary_dir:
            parser = Hl7WorklistParser()
            builder = DicomWorklistBuilder(folder = temporary_dir)
            orm_handler = Hl7OrmWorklistMsgHandler(parser=parser, builder=builder)

            mllp_server = MLLPServer(
                    host = 'localhost',
                    port = port_number,
                    handlers = {
                        'ORM^O01': (orm_handler.handle_orm_message,)
                    },
                    logger = logging.getLogger('WORKLIST SERVER')
            )

            mllp_server.add_handlers({'ORM^O01': (orm_handler.handle_orm_message,)})

            with mllp_server as server:
                # validate that ORM messages do create worklist files
                with MLLPClient('localhost', port_number) as client:
                    hl7_request = hl7.parse(
                        "\x0bMSH|^~\&|myhospital.org|myhospital.org|||2017-04-25 07:31:13.123456||ORM^O01|269539|P|2.3.1|||||||||\r"
                        "PID|||1234567^^^myhospital.org||VANILL\xc9^LAURA^^^Mme^^L|MAIDEN^^^^^^L|19521103|F|||RUE MARIE CURIE^BRUXELLES^^74850^99100..LONG ADDRESS..1234567890123456789012345678901234567890|||||D|||272110608803615||||||||||20150930000000|Y|\r"
                        "PV1||N||||||REF^DOCTOR^JULIEN|||||||||\r"
                        "ORC|NW|723085|269539||SC|||||||CHUFJEA^CHIFREZE^JEAN FRANCOIS||\r"
                        "OBR||269539|269539|SC3TER.INJ^SCANNER \xc9 DE 3 TERRITOIRES ANATOMIQUES OU PLUS AVEC INJECTION \xe9||||||||||||||269539|269539||^^^^SCAN|||CT|||^^^201709141537^^R|||||||^^^^SCAN|\r"
                        "OBX||ST|^BODY WEIGHT||62|kg|||||F\r"
                        "OBX||ST|^BODY HEIGHT||1.90|m|||||F\r"
                        "\x1c\x0d"
                    )
                    response = client.send(hl7_request)
                    hl7_response = hl7.parse(response)

                # make sure a file has been created
                files = glob.glob('{path}/*.wl'.format(path = temporary_dir))
                self.assertEqual(1, len(files))
                worklist_file_path = files[0]

                # check the content of the file
                wl = pydicom.read_file(worklist_file_path)
                self.assertEqual("VANILLÉ^LAURA^^^Mme", wl.PatientName)
                self.assertEqual("19521103", wl.PatientBirthDate)
                self.assertEqual("ISO_IR 100", wl.SpecificCharacterSet)  # default char set if not specified in HL7 message
                self.assertEqual("SCANNER É DE 3 TERRITOIRES ANATOMIQUES OU PLUS AVEC INJECTION é", wl.RequestedProcedureDescription)
                self.assertEqual("CHUFJEA^CHIFREZE^JEAN FRANCOIS", wl.RequestingPhysician)
                self.assertEqual("MAIDEN", wl.PatientMotherBirthName.family_name)

                self.assertEqual("CT", wl.ScheduledProcedureStepSequence[0].Modality)
                self.assertEqual("20170914", wl.ScheduledProcedureStepSequence[0].ScheduledProcedureStepStartDate)
                # make sure all 'mandatory' fields are there
                self.assertEqual("723085", wl.ScheduledProcedureStepSequence[0].ScheduledProcedureStepID)
                self.assertEqual("UNKNOWN", wl.ScheduledProcedureStepSequence[0].ScheduledStationAETitle)
                self.assertEqual("REF^DOCTOR^JULIEN", wl.ReferringPhysicianName)
                self.assertEqual(0, len(wl.ReferencedStudySequence))
                self.assertEqual(0, len(wl.ReferencedPatientSequence))
                self.assertEqual("723085", wl.RequestedProcedureID)
                self.assertEqual("RUE MARIE CURIE^BRUXELLES^^74850^99100..LONG ADDRESS..123456...", wl.PatientAddress)

    def test_from_q_doc_chu_liege(self):
        port_number = 2003  # there are currently some issues when trying to reuse the same port in 2 tests (it's probably not freed soon enough -> let's use another port for each test)
        with tempfile.TemporaryDirectory() as temporary_dir:
            parser = Hl7WorklistParser({'AccessionNumber': 'OBR.F18'})
            builder = DicomWorklistBuilder(folder = temporary_dir)
            orm_handler = Hl7OrmWorklistMsgHandler(parser=parser, builder=builder)

            with MLLPServer(
                    host = 'localhost',
                    port = port_number,
                    handlers = {
                    'ORM^O01': (orm_handler.handle_orm_message,)
                    },
                    logger = logging.getLogger('WORKLIST SERVER')
            ) as server:
                # validate that ORM messages do create worklist files
                with MLLPClient('localhost', port_number) as client:
                    source_binary_message = (b"\x0bMSH|^~\&|QDOC|HL7V1.1|AGFA|AGFA|20170505112549||ORM^O01|03139638|P|2.3.1||||||8859/1\r"
                                           b"PID|||123456Q||DUBOIS^Jean||19201231|M|||RUE DE LA STATION 14^^VILLAGE^^4999^BE||||||||12345678901\r"
                                           b"ORC|SC||N4568254^NDB||IP||^^^20170505111800^^R|||C123456||123456^DOCTEUR^NICOLA|||||^^L\r"
                                           b"OBR|||N4568254^NDB|CTCRANE^CT c\xe9r\xe9bral^QDOC^^^QUADRAT||20170505111800|20170505111800||||||||^^^NEURO CERV|123456^DOCTEUR^NICOLA||0897456|0897456|0897456|||||||^^^20170505111800^^R|||||||^^^NDB^CT NDB||20170505111800\r"
                                           b"ZDS|1.2.41.0.1.1.202.123.42.21.5832143.5832122^Agfa^Application^DICOM\r"
                                           b"\x1c\x0d")

                    response = client.send(source_binary_message)

                # make sure a file has been created
                worklist_file_path = os.path.join(temporary_dir, '0897456.wl')
                self.assertTrue(os.path.isfile(worklist_file_path))

                # check the content of the file
                wl = pydicom.read_file(worklist_file_path)
                self.assertEqual("DUBOIS^Jean", wl.PatientName)
                self.assertEqual("19201231", wl.PatientBirthDate)
                self.assertEqual("ISO_IR 100", wl.SpecificCharacterSet)
                self.assertEqual("CT cérébral", wl.RequestedProcedureDescription)

                # check that the specific field is correctly handled
                self.assertEqual("0897456", wl.AccessionNumber)

    def test_orthanc_worklist_c_find_encoding_bug(self):
        port_number = 2004  # there are currently some issues when trying to reuse the same port in 2 tests (it's probably not freed soon enough -> let's use another port for each test)
        with tempfile.TemporaryDirectory() as temporary_dir:
            parser = Hl7WorklistParser()
            builder = DicomWorklistBuilder(folder = temporary_dir)
            orm_handler = Hl7OrmWorklistMsgHandler(parser=parser, builder=builder)

            with MLLPServer(
                    host='localhost',
                    port=port_number,
                    handlers={
                        'ORM^O01': (orm_handler.handle_orm_message,)
                    },
                    logger=logging.getLogger('WORKLIST SERVER')
            ) as server:
                # validate that ORM messages do create worklist files
                with MLLPClient('localhost', port_number) as client:
                    hl7_request = hl7.parse(
                        "\x0bMSH|^~\&|myhospital.org|myhospital.org|||2017-06-08 07:31:13.123456||ORM^O01|123456|P|2.3.1|||||||||\r"
                        "PID|||201102956^^^myhospital.org||VANILL\xc9^LAURA^^^Mme^^L|MAIDEN^^^^^^L|19521103|F|||RUE MARIE CURIE^BRUXELLES^^74850^99100|||||D|||272110608803615||||||||||20150930000000|Y|\r"
                        "PV1||N|||||||||||||||\r"
                        "ORC|NW|723085|123456||SC|||||||DOCTOR_CODE^DOCTOR^NAME||\r"
                        "OBR||123456|123456|STUDY_CODE^\xc9CHOGRAPHIE||||||||||||||123456|123456||^^^^SCAN|||CT|||^^^201706081537^^R|||||||^^^^SCAN|\r"
                        "\x1c\x0d"
                    )
                    response = client.send(hl7_request)
                    hl7Response = hl7.parse(response)

                # make sure a file has been created
                files = glob.glob('{path}/*.wl'.format(path = temporary_dir))
                self.assertEqual(1, len(files))
                worklist_file_path = files[0]

                # check the content of the file
                wl = pydicom.read_file(worklist_file_path)
                self.assertEqual("VANILLÉ^LAURA^^^Mme", wl.PatientName)
                self.assertEqual("MAIDEN^^^^^^L", wl.PatientMotherBirthName)
                self.assertEqual("ISO_IR 100", wl.SpecificCharacterSet)  # default char set if not specified in HL7 message
                self.assertEqual("ÉCHOGRAPHIE", wl.RequestedProcedureDescription)
                self.assertEqual("DOCTOR_CODE^DOCTOR^NAME", wl.RequestingPhysician)
                self.assertEqual("CT", wl.ScheduledProcedureStepSequence[0].Modality)
                self.assertEqual("20170608", wl.ScheduledProcedureStepSequence[0].ScheduledProcedureStepStartDate)

    def test_ried_worklists(self):
        port_number = 2005  # there are currently some issues when trying to reuse the same port in 2 tests (it's probably not freed soon enough -> let's use another port for each test)
        with tempfile.TemporaryDirectory() as temporary_dir:
            parser = Hl7WorklistParser()
            builder = DicomWorklistBuilder(folder = temporary_dir)
            orm_handler = Hl7OrmWorklistMsgHandler(parser=parser, builder=builder)

            mllp_server = MLLPServer(
                    host = 'localhost',
                    port = port_number,
                    handlers = {
                    'ORM^O01^ORM_O01': (orm_handler.handle_orm_message,)
                    },
                    logger = logging.getLogger('WORKLIST SERVER')
            )

            with mllp_server as server:
                # validate that ORM messages do create worklist files
                with MLLPClient('localhost', port_number) as client:
                    hl7_request = hl7.parse(
                        "\x0bMSH|^~\&|ECSIMAGING|CORADIX|BEA|BEA|20201001140735||ORM^O01^ORM_O01|6af22cb1-38af-4dc7-93d5-83e749394237|P|2.3.1|||||FRA|8859/15|FRA||\r"
                        "PID|1||5343197^^^ECSIMAGING^PI||LLOxxx^Simxxx^^^^^D~LLOxxx^Simxxx^^^^^L||19550812000000|F|||2 rue ^^THUIR^^66300^^H||^^PH^^^^^^^^^0404040404~^^CP^^^^^^^^^0606060606|||U||A1.02412251^^^ECSIMAGING^AN||||||||^^||^^||\r"
                        "PV1||O||R||||Docteur^Traitant|||||||||||A1.02412251^^^ECSIMAGING^VN|||||||||||||||||||||||||20201001134400||||||||\r"
                        "ORC|NW|3264557^ECSIMAGING|3264557^ECSIMAGING|2412251^ECSIMAGING|||1^^^20201001141000|||||Docteur^Quenotte|||20201001141000||||||||||\r"
                        "OBR|1||3264557^ECSIMAGING|I90 FOIE IV^IRM FOIE IV^ECSIMAGING^ZCQJ004^IRM FOIE IV^CCAM||||||||||||Docteur^Quenotte||||||||NMR|||1^^20^20201001141000\r"
                        "ZDS|1.3.6.1.4.1.31672.1.2.1.973852.91.1596520991.411\r"
                        "\x1c\x0d"
                    )
                    response = client.send(hl7_request)
                    hl7Response = hl7.parse(response)

                # make sure a file has been created
                files = glob.glob('{path}/*.wl'.format(path = temporary_dir))
                self.assertEqual(1, len(files))
                worklist_file_path = files[0]

                # check the content of the file
                wl = pydicom.read_file(worklist_file_path)
                self.assertEqual("LLOxxx^Simxxx^^^", wl.PatientName)
                self.assertEqual("19550812", wl.PatientBirthDate)
                self.assertEqual("ISO_IR 100", wl.SpecificCharacterSet)  # default char set if not specified in HL7 message
                self.assertEqual("IRM FOIE IV", wl.RequestedProcedureDescription)
                self.assertEqual("Docteur^Quenotte", wl.RequestingPhysician)
                self.assertEqual("NMR", wl.ScheduledProcedureStepSequence[0].Modality)
                self.assertEqual("20201001", wl.ScheduledProcedureStepSequence[0].ScheduledProcedureStepStartDate)

                # make sure all 'mandatory' fields are there
                self.assertEqual("3264557", wl.ScheduledProcedureStepSequence[0].ScheduledProcedureStepID)
                self.assertEqual("UNKNOWN", wl.ScheduledProcedureStepSequence[0].ScheduledStationAETitle)
                self.assertEqual("Docteur^Traitant", wl.ReferringPhysicianName)
                self.assertEqual("3264557", wl.RequestedProcedureID)
                self.assertEqual("2 rue ^^THUIR^^66300^^H", wl.PatientAddress)
                self.assertEqual("3264557", wl.AccessionNumber)