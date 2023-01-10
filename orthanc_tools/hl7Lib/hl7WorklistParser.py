import contextlib
import typing
# from hl7Lib import Hl7MessageParser
from .hl7MessageParser import Hl7MessageParser


class Hl7WorklistParser(Hl7MessageParser):

    def __init__(self):
        super(Hl7WorklistParser, self).__init__()

        self.add_fields_definitions({
            # --- PID segment
            'PatientID': 'PID.F3.R1.C1',
            'IssuerOfPatientID': 'PID.F3.R1.C4',
            'PatientName': 'PID.F5',  # IHE recommend to use the whole Field 5 as the patient name (it's usually full of ^ that is a separator in HL7 so we must take it as is without trying to parse it !
            'PatientMotherBirthName': 'PID.F6',
            'PatientBirthDate': 'PID.F7',
            '_sex': 'PID.F8',
            'PatientAddress' : 'PID.F11',

            # --- OBR segment
            #'AccessionNumber': 'OBR.F18',
            'AccessionNumber': 'OBR.F3.R1.C1',
            'PatientState': 'OBR.F12',
            '_requestingPhysicianOBR': 'OBR.F16',
            'ReasonForTheRequestedProcedure': 'OBR.F31',
            'Modality': 'OBR.F24',
            'RequestedProcedureDescription': 'OBR.F4.R1.C2',
            #'ScheduledProcedureStepStartDateTime': 'OBR.F27.R1.C4',
            '_scheduledProcedureStepStartDateTime': 'OBR.F27.R1.C4',

            # --- PV1 segment
            '_ambulatoryStatus' : 'PV1.F15',
            'ReferringPhysicianName' : 'PV1.F8',
            'ConfidentialityConstraintOnPatientDataDescription' : 'PV1.F16',

            # --- ORC segment
            'OrderPlacerIdentifierSequence' : 'ORC.F2',
            'RequestedProcedureID' : 'ORC.F2',
            'ScheduledProcedureStepID' : 'ORC.F2',
            'OrderFillerIdentifierSequence' : 'ORC.F3',
            '_requestingPhysicianORC' : 'ORC.F12',

            # --- ZDS segment
            'StudyInstanceUID' : 'ZDS.F1.R1.C1'
        })

    def parse(self, hl7_message: str) -> typing.Dict:

        # set a bunch of default values to make sure worklists are accepted by some GE modalities
        # REQUIRED elements
        values = {}
        values['AccessionNumber'] = None
        values['RequestingPhysician'] = None
        values['RequestedProcedureDescription'] = None
        values['Modality'] = None
        values['ReferringPhysicianName'] = None
        values['ScheduledStationAETitle'] = 'UNKNOWN'
        values['ScheduledPerformingPhysicianName'] = None
        values['ScheduledStationName'] = None
        values['ScheduledProcedureStepID'] = 'UNKNOWN'
        values['RequestedProcedureID'] = 'UNKNOWN'

        # extract field the default way
        values_from_hl7 = super(Hl7WorklistParser, self).parse(hl7_message, strict = False)

        values.update(values_from_hl7)

        # keep only the first 5 components of the name according to http://dicom.nema.org/dicom/2013/output/chtml/part05/sect_6.2.html (check PN VR definition)
        values['PatientName'] = '^'.join(values['PatientName'].split('^')[:5])

        sex = values['_sex']
        if sex is None or sex in ['M', 'F']:
            values['PatientSex'] = sex
        elif sex in ['U']:  # unknown in HL7 -> null in Dicom
            values['PatientSex'] = None
        elif sex in ['A', 'N']:  # ambiguous or Not Applicable in HL7 -> 'other' in Dicom
            values['PatientSex'] = 'O'

        if values.get('_ambulatoryStatus') is not None and 'B6' in values['_ambulatoryStatus']:
            values['PregnancyStatus'] = 3

        # --- OBX segment parsing
        with contextlib.suppress(KeyError):  # OBX segments might not be there
            for i in range(0, len(self._hl7_message['OBX'])):
                observation = self._get_whole_field('OBX.F3', segment_index = i)
                if 'BODY WEIGHT' in observation.upper():
                    values['PatientWeight'] = self._get_whole_field('OBX.F5', segment_index = i)
                if 'BODY HEIGHT' in observation.upper():
                    values['PatientHeight'] = self._get_whole_field('OBX.F5', segment_index = i)

        if values.get('_encoding') in [None, '8859/1', '8859/15']:
            values['SpecificCharacterSet'] = 'ISO_IR 100'

        if values.get('_scheduledProcedureStepStartDateTime') is not None:
            datetimeString = values.get('_scheduledProcedureStepStartDateTime')
            values['ScheduledProcedureStepStartDate'] = datetimeString[:8]  # date is made of the 8 first chars of the string
            if len(datetimeString) == 12:
                values['ScheduledProcedureStepStartTime'] = datetimeString[8:12] + "00"
            elif len(datetimeString) == 14:
                values['ScheduledProcedureStepStartTime'] = datetimeString[8:14]

        if values.get('_requestingPhysicianOBR') is not None:
            values['RequestingPhysician'] = values.get('_requestingPhysicianOBR')
        elif values.get('_requestingPhysicianORC') is not None:
            values['RequestingPhysician'] = values.get('_requestingPhysicianORC')

        return values

