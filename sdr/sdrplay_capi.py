"""
SDRplay API v3 — ctypes binding
Based on: headers under /Library/SDRplayAPI/3.15.1/include/sdrplay_api*.h

Pure struct/function-signature layer, no I/O or threading — mirrors how
amplifier/acom_protocol.py is the pure-protocol counterpart to
amplifier/acom_serial.py's I/O driver.

Struct field order/types must exactly match the C headers — ctypes computes
offsets from declaration order, and several structs here are embedded by
value inside larger structs (e.g. RxChannelParamsT), so a wrong field
anywhere shifts every field that follows it in memory.
"""

import ctypes as C

DEFAULT_LIB_PATH = "/usr/local/lib/libsdrplay_api.dylib"

HANDLE = C.c_void_p
SDRPLAY_MAX_SER_NO_LEN = 64
SDRPLAY_MAX_DEVICES = 16

# Hardware version IDs (sdrplay_api.h)
SDRPLAY_RSP1_ID = 1
SDRPLAY_RSP1A_ID = 255
SDRPLAY_RSP2_ID = 2
SDRPLAY_RSPduo_ID = 3
SDRPLAY_RSPdx_ID = 4
SDRPLAY_RSP1B_ID = 6
SDRPLAY_RSPdxR2_ID = 7

# Tuner bandwidth / IF enums (sdrplay_api_tuner.h)
BW_0_200 = 200
BW_0_300 = 300
BW_0_600 = 600
BW_1_536 = 1536
BW_5_000 = 5000
BW_6_000 = 6000
BW_7_000 = 7000
BW_8_000 = 8000

IF_Undefined = -1
IF_Zero = 0
IF_0_450 = 450
IF_1_620 = 1620
IF_2_048 = 2048

AGC_DISABLE = 0
AGC_100HZ = 1
AGC_50HZ = 2
AGC_5HZ = 3
AGC_CTRL_EN = 4

# RSPdx antenna select (sdrplay_api_rspDx.h)
RspDx_ANTENNA_A = 0
RspDx_ANTENNA_B = 1
RspDx_ANTENNA_C = 2

# Reason-for-update bit flags (sdrplay_api.h) — only the ones we use
Update_Dev_Fs = 0x00000001
Update_Tuner_Gr = 0x00008000
Update_Tuner_Frf = 0x00020000
Update_Tuner_BwType = 0x00040000
Update_Tuner_IfType = 0x00080000
Update_Ext1_None = 0x00000000

Tuner_A = 1
Tuner_Neither = 0

# Event IDs (sdrplay_api_callback.h)
Overload_Detected = 0
Overload_Corrected = 1
Event_GainChange = 0
Event_PowerOverloadChange = 1
Event_DeviceRemoved = 2
Event_RspDuoModeChange = 3
Event_DeviceFailure = 4


# ---------------------------------------------------------------------------
# Structs — leaf-to-root order (children must be defined before parents)
# ---------------------------------------------------------------------------

class GainValuesT(C.Structure):
    _fields_ = [("curr", C.c_float), ("max", C.c_float), ("min", C.c_float)]


class GainT(C.Structure):
    _fields_ = [("gRdB", C.c_int), ("LNAstate", C.c_ubyte), ("syncUpdate", C.c_ubyte),
                ("minGr", C.c_int), ("gainVals", GainValuesT)]


class RfFreqT(C.Structure):
    _fields_ = [("rfHz", C.c_double), ("syncUpdate", C.c_ubyte)]


class DcOffsetTunerT(C.Structure):
    _fields_ = [("dcCal", C.c_ubyte), ("speedUp", C.c_ubyte),
                ("trackTime", C.c_int), ("refreshRateTime", C.c_int)]


class TunerParamsT(C.Structure):
    _fields_ = [("bwType", C.c_int), ("ifType", C.c_int), ("loMode", C.c_int),
                ("gain", GainT), ("rfFreq", RfFreqT), ("dcOffsetTuner", DcOffsetTunerT)]


class DcOffsetT(C.Structure):
    _fields_ = [("DCenable", C.c_ubyte), ("IQenable", C.c_ubyte)]


class DecimationT(C.Structure):
    _fields_ = [("enable", C.c_ubyte), ("decimationFactor", C.c_ubyte), ("wideBandSignal", C.c_ubyte)]


class AgcT(C.Structure):
    _fields_ = [("enable", C.c_int), ("setPoint_dBfs", C.c_int), ("attack_ms", C.c_ushort),
                ("decay_ms", C.c_ushort), ("decay_delay_ms", C.c_ushort),
                ("decay_threshold_dB", C.c_ushort), ("syncUpdate", C.c_int)]


class ControlParamsT(C.Structure):
    _fields_ = [("dcOffset", DcOffsetT), ("decimation", DecimationT),
                ("agc", AgcT), ("adsbMode", C.c_int)]


class Rsp1aTunerParamsT(C.Structure):
    _fields_ = [("biasTEnable", C.c_ubyte)]


class Rsp2TunerParamsT(C.Structure):
    _fields_ = [("biasTEnable", C.c_ubyte), ("amPortSel", C.c_int),
                ("antennaSel", C.c_int), ("rfNotchEnable", C.c_ubyte)]


class RspDuoResetSlaveFlagsT(C.Structure):
    _fields_ = [("resetGainUpdate", C.c_ubyte), ("resetRfUpdate", C.c_ubyte)]


class RspDuoTunerParamsT(C.Structure):
    _fields_ = [("biasTEnable", C.c_ubyte), ("tuner1AmPortSel", C.c_int),
                ("tuner1AmNotchEnable", C.c_ubyte), ("rfNotchEnable", C.c_ubyte),
                ("rfDabNotchEnable", C.c_ubyte), ("resetSlaveFlags", RspDuoResetSlaveFlagsT)]


class RspDxTunerParamsT(C.Structure):
    _fields_ = [("hdrBw", C.c_int)]


class RxChannelParamsT(C.Structure):
    _fields_ = [("tunerParams", TunerParamsT), ("ctrlParams", ControlParamsT),
                ("rsp1aTunerParams", Rsp1aTunerParamsT), ("rsp2TunerParams", Rsp2TunerParamsT),
                ("rspDuoTunerParams", RspDuoTunerParamsT), ("rspDxTunerParams", RspDxTunerParamsT)]


class FsFreqT(C.Structure):
    _fields_ = [("fsHz", C.c_double), ("syncUpdate", C.c_ubyte), ("reCal", C.c_ubyte)]


class SyncUpdateT(C.Structure):
    _fields_ = [("sampleNum", C.c_uint), ("period", C.c_uint)]


class ResetFlagsT(C.Structure):
    _fields_ = [("resetGainUpdate", C.c_ubyte), ("resetRfUpdate", C.c_ubyte), ("resetFsUpdate", C.c_ubyte)]


class Rsp1aParamsT(C.Structure):
    _fields_ = [("rfNotchEnable", C.c_ubyte), ("rfDabNotchEnable", C.c_ubyte)]


class Rsp2ParamsT(C.Structure):
    _fields_ = [("extRefOutputEn", C.c_ubyte)]


class RspDuoParamsT(C.Structure):
    _fields_ = [("extRefOutputEn", C.c_int)]


class RspDxParamsT(C.Structure):
    _fields_ = [("hdrEnable", C.c_ubyte), ("biasTEnable", C.c_ubyte), ("antennaSel", C.c_int),
                ("rfNotchEnable", C.c_ubyte), ("rfDabNotchEnable", C.c_ubyte)]


class DevParamsT(C.Structure):
    _fields_ = [("ppm", C.c_double), ("fsFreq", FsFreqT), ("syncUpdate", SyncUpdateT),
                ("resetFlags", ResetFlagsT), ("mode", C.c_int), ("samplesPerPkt", C.c_uint),
                ("rsp1aParams", Rsp1aParamsT), ("rsp2Params", Rsp2ParamsT),
                ("rspDuoParams", RspDuoParamsT), ("rspDxParams", RspDxParamsT)]


class DeviceParamsT(C.Structure):
    _fields_ = [("devParams", C.POINTER(DevParamsT)), ("rxChannelA", C.POINTER(RxChannelParamsT)),
                ("rxChannelB", C.POINTER(RxChannelParamsT))]


class DeviceT(C.Structure):
    _fields_ = [("SerNo", C.c_char * SDRPLAY_MAX_SER_NO_LEN), ("hwVer", C.c_ubyte),
                ("tuner", C.c_int), ("rspDuoMode", C.c_int), ("valid", C.c_ubyte),
                ("rspDuoSampleFreq", C.c_double), ("dev", HANDLE)]


class StreamCbParamsT(C.Structure):
    _fields_ = [("firstSampleNum", C.c_uint), ("grChanged", C.c_int), ("rfChanged", C.c_int),
                ("fsChanged", C.c_int), ("numSamples", C.c_uint)]


class GainCbParamT(C.Structure):
    _fields_ = [("gRdB", C.c_uint), ("lnaGRdB", C.c_uint), ("currGain", C.c_double)]


class PowerOverloadCbParamT(C.Structure):
    _fields_ = [("powerOverloadChangeType", C.c_int)]


class RspDuoModeCbParamT(C.Structure):
    _fields_ = [("modeChangeType", C.c_int)]


class EventParamsT(C.Union):
    _fields_ = [("gainParams", GainCbParamT), ("powerOverloadParams", PowerOverloadCbParamT),
                ("rspDuoModeParams", RspDuoModeCbParamT)]


StreamCallback_t = C.CFUNCTYPE(None, C.POINTER(C.c_short), C.POINTER(C.c_short),
                                C.POINTER(StreamCbParamsT), C.c_uint, C.c_uint, C.c_void_p)
EventCallback_t = C.CFUNCTYPE(None, C.c_int, C.c_int, C.POINTER(EventParamsT), C.c_void_p)


class CallbackFnsT(C.Structure):
    _fields_ = [("StreamACbFn", StreamCallback_t), ("StreamBCbFn", StreamCallback_t),
                ("EventCbFn", EventCallback_t)]


class ErrorInfoT(C.Structure):
    _fields_ = [("file", C.c_char * 256), ("function", C.c_char * 256),
                ("line", C.c_int), ("message", C.c_char * 1024)]


def load_library(lib_path: str = DEFAULT_LIB_PATH) -> C.CDLL:
    """Load libsdrplay_api and declare the function signatures we use."""
    lib = C.CDLL(lib_path)

    lib.sdrplay_api_Open.restype = C.c_int
    lib.sdrplay_api_Close.restype = C.c_int
    lib.sdrplay_api_ApiVersion.argtypes = [C.POINTER(C.c_float)]
    lib.sdrplay_api_ApiVersion.restype = C.c_int
    lib.sdrplay_api_GetDevices.argtypes = [C.POINTER(DeviceT), C.POINTER(C.c_uint), C.c_uint]
    lib.sdrplay_api_GetDevices.restype = C.c_int
    lib.sdrplay_api_SelectDevice.argtypes = [C.POINTER(DeviceT)]
    lib.sdrplay_api_SelectDevice.restype = C.c_int
    lib.sdrplay_api_ReleaseDevice.argtypes = [C.POINTER(DeviceT)]
    lib.sdrplay_api_ReleaseDevice.restype = C.c_int
    lib.sdrplay_api_GetDeviceParams.argtypes = [HANDLE, C.POINTER(C.POINTER(DeviceParamsT))]
    lib.sdrplay_api_GetDeviceParams.restype = C.c_int
    lib.sdrplay_api_Init.argtypes = [HANDLE, C.POINTER(CallbackFnsT), C.c_void_p]
    lib.sdrplay_api_Init.restype = C.c_int
    lib.sdrplay_api_Uninit.argtypes = [HANDLE]
    lib.sdrplay_api_Uninit.restype = C.c_int
    lib.sdrplay_api_Update.argtypes = [HANDLE, C.c_int, C.c_uint, C.c_uint]
    lib.sdrplay_api_Update.restype = C.c_int
    lib.sdrplay_api_GetErrorString.argtypes = [C.c_int]
    lib.sdrplay_api_GetErrorString.restype = C.c_char_p
    lib.sdrplay_api_DisableHeartbeat.restype = C.c_int

    return lib


def error_string(lib: C.CDLL, err: int) -> str:
    return lib.sdrplay_api_GetErrorString(err).decode(errors="replace")
