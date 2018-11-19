from random import randint
import threading
import manchester
import crc
from packet import Packet
from enum import Enum
import logging
from message import Message, MessageState
from queue import Queue, Empty

from rflib import (RfCat, ChipconUsbTimeoutException, MOD_2FSK, SYNCM_CARRIER_16_of_16, SYNCM_NONE,
                    MFMCFG1_NUM_PREAMBLE0, MFMCFG1_NUM_PREAMBLE_2, SYNCM_CARRIER)

class CommunicationError(Exception):
    pass

class ProtocolError(Exception):
    pass

class RadioMode(Enum):
    Sniffer = 0,
    Pdm = 1,
    Pod = 2

class Radio:
    def __init__(self, usbInterface = 0, msgSequence = 0, pktSequence = 0):
        self.stopRadioEvent = threading.Event()
        self.usbInterface = usbInterface
        self.manchester = manchester.ManchesterCodec()
        self.messageSequence = msgSequence
        self.packetSequence = pktSequence

    def __logPacket(self, p):
        logging.debug("Packet received: %s", p)

    def __logMessage(self, msg):
        logging.debug("Message received: %s", msg)
        return None

    def start(self, packetReceivedCallback = None, messageCallback = None, radioMode = RadioMode.Sniffer, address = None, sendPacketLength = 512):
        logging.debug("starting radio in %s", radioMode)
        self.lastPacketReceived = None
        self.addressToCheck = address
        self.responseTimeout = 1000
        self.radioMode = radioMode
        self.radioPacketLength = sendPacketLength
        self.__initializeRfCat()

        if packetReceivedCallback is None:
            self.packetReceivedCallback = self.__logPacket
        else:
            self.packetReceivedCallback = packetReceivedCallback

        if messageCallback is None:
            self.messageCallback = self.__logMessage
        else:
            self.messageCallback = messageCallback

        if radioMode == RadioMode.Sniffer:
            self.radioThread = threading.Thread(target = self.__snifferLoop)
            self.radioThread.start()
        elif radioMode == RadioMode.Pod:
            self.radioThread = threading.Thread(target = self.__podLoop)
            self.radioThread.start()

    def stop(self):
        if self.radioMode == RadioMode.Sniffer or self.radioMode == RadioMode.Pod:
            self.stopRadioEvent.set()
            self.radioThread.join()
        self.rfc.cleanup()

    def __initializeRfCat(self):
        self.rfc = RfCat(self.usbInterface, debug=False)
        #rfc.setModeIDLE()
        self.rfc.setFreq(433.91e6)
        self.rfc.setMdmModulation(MOD_2FSK)
        self.rfc.setMdmDeviatn(26370)
        self.rfc.setPktPQT(1)
        self.rfc.setEnableMdmManchester(False)
        self.rfc.setMdmDRate(40625)
        self.rfc.setRFRegister(0xdf18, 0x70)

        self.rfc.setMdmSyncMode(SYNCM_CARRIER_16_of_16)
        self.rfc.setMdmNumPreamble(MFMCFG1_NUM_PREAMBLE_2)
        self.rfc.setMdmSyncWord(0xa55a)
        self.rfc.makePktFLEN(80)

# AB3C actual sync word before manchester encoding
# after encoding: 6665a55a
# actual encoded packet looks like this:

# 0x6665 (repeated > 200 times) 0xa55a 

# possible syncwords:
# 0x5a
# 0xa55a
# 0x65a55a
# 0x6665a55a
# 0x656665a55a
# 0x66656665a55a
# etc..


    def __snifferLoop(self):
        q = Queue(4096)
        while not self.stopRadioEvent.wait(0):
            try:
                while True:
                    rfdata = self.rfc.RFrecv(timeout = 1500)
                    if rfdata is None:
                        break
                    q.put(rfdata)
            except:
                pass

            try:
                while True:
                    rfd = q.get_nowait()
                    p = self.__getPacket(rfd)
                    if p is not None and (self.lastPacketReceived is None or self.lastPacketReceived.sequence != p.sequence):
                        self.lastPacketReceived = p
                        self.packetReceivedCallback(p)
            except Empty:
                pass

    def __podLoop(self):
        pass

    def __receive(self, timeout):
        rfdata = None
        try:
            rfdata = self.rfc.RFrecv(timeout = timeout)
        except ChipconUsbTimeoutException:
            rfdata = None
        return rfdata

    def __send(self, data):
        success = False
        try:
            self.rfc.RFxmit(data)
        except ChipconUsbTimeoutException:
            pass
        return success

    def sendRequestToPod(self, message, responseHandler = None):
        while True:
            message.sequence = self.messageSequence
            #logging.debug("SENDING: %s" % message)
            packets = message.getPackets()
            received = None

            for i in range(0, len(packets)):
                packet = packets[i]
                if i == len(packets)-1:
                    exp = "POD"
                else:
                    exp = "ACK"
                received = self.__sendPacketAndGetPacketResponse(packet, exp)
                if received is None:
                    raise CommunicationError()

            podResponse = Message.fromPacket(received)
            if podResponse is None:
                raise ProtocolError()

            while podResponse.state == MessageState.Incomplete:
                ackPacket = Packet.Ack(message.address, False)
                received = self.__sendPacketAndGetPacketResponse(ackPacket, "CON")
                podResponse.addConPacket(received)

            if podResponse.state == MessageState.Invalid:
                raise ProtocolError()

            #logging.debug("RECEIVED: %s" % podResponse)
            respondResult = None
            if responseHandler is not None:
                respondResult = responseHandler(message, podResponse)

            if respondResult is None:
                ackPacket = Packet.Ack(message.address, True)
                self.__sendPacketUntilQuiet(ackPacket)
                self.messageSequence = (podResponse.sequence + 1) % 16
                return podResponse
            else:
                message = respondResult

    def __sendPacketUntilQuiet(self, packetToSend):
        packetToSend.setSequence(self.packetSequence)
        logging.debug("SENDING: %s" % packetToSend)
        data = packetToSend.data
        data += chr(crc.crc8(data))
        data = self.manchester.encode(data, self.radioPacketLength)
        sendTimes = 0
        for i in range(0, 10):
            self.__send(data)
            rfData = self.__receive(timeout = 500)
            if rfData is None:
                self.packetSequence = (self.packetSequence + 1) % 32
                return
            sendTimes += 1

    def __sendPacketAndGetPacketResponse(self, packetToSend, expectedType):
        expectedAddress = packetToSend.address
        longTimeout = 0
        loopies = 0
        while loopies < 3:
            packetToSend.setSequence(self.packetSequence)
            logging.debug("SENDING: %s" % packetToSend)
            data = packetToSend.data
            data += chr(crc.crc8(data))
            data = self.manchester.encode(data, self.radioPacketLength)
            retries = 0
            expectedSequence = (packetToSend.sequence + 1) % 32
            while retries < 20:
                retries += 1
                self.__send(data)
                if longTimeout == 0:
                    tmout = randint(1000, 1300)
                else:
                    tmout = longTimeout
                    longTimeout = 0
                rfData = self.__receive(timeout = tmout)
                if rfData is not None:
                    p = self.__getPacket(rfData)
                    if p is not None and p.address == expectedAddress:
                        logging.debug("RECEIVED: %s" % p)
                        if p.type == expectedType:
                            self.packetSequence = (p.sequence + 1) % 32
                            return p
                        loopies += 1
                        if loopies == 1:
                            longTimeout = randint(4700,5300)
                        else:
                            longTimeout = randint(9700, 10300)
                            self.packetSequence = (self.packetSequence + 31) % 32
                        break
        raise ProtocolError()

    def __getPacket(self, rfdata):
        data, timestamp = rfdata
        data = self.manchester.decode(data)
        if data is not None and len(data) > 1:
            calc = crc.crc8(data[0:-1])
            if ord(data[-1]) == calc:
                return Packet(timestamp, data[:-1])
        return None