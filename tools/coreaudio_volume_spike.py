#!/usr/bin/env python3
"""
Spike: does any CoreAudio device expose a settable HAL output volume?

This answers one yes/no question for the TX-power-target closed-loop plan
(see the ACOM-amp control-loop discussion): WSJT-X's own "Pwr" slider is
just Qt's QAudioOutput::setVolume() on its outgoing audio stream — so an
OS-level output-volume control on the specific audio device feeding the
FT-991A's data/mic input would have the *same physical effect*, reachable
from our own console without ever touching WSJT-X's code. Whether the
FT-991A's USB audio codec actually implements a settable volume property
(some simple class-compliant USB audio devices report a fixed/passthrough
level with no settable volume at all) can only be answered by querying the
live device — this script does that query.

This is a standalone, read-mostly diagnostic — not imported by the app.
Talks directly to macOS CoreAudio's HAL via ctypes (no pyobjc dependency,
deliberately, so answering this question doesn't obligate the rest of the
project to a new pip dependency).

Usage:
    ./venv/bin/python tools/coreaudio_volume_spike.py
        Lists every CoreAudio device with its name and whether a
        master/per-channel output volume property exists and is settable.
        Read-only — makes no Set calls, so nothing's volume actually
        changes (safe to run any time, changes nothing audible).

    ./venv/bin/python tools/coreaudio_volume_spike.py --test "USB"
        Same listing, then additionally round-trip tests (set, read back,
        restore) the output volume on whichever device name contains the
        given substring (case-insensitive) — only if exactly one device
        matches, to avoid guessing on the wrong device. This is the step
        that actually proves settability end-to-end, not just the
        HAL's IsPropertySettable flag. Run this with the FT-991A's USB
        audio device name once you've identified it from the plain
        listing above (check Audio MIDI Setup.app for the exact name if
        unsure).

    ./venv/bin/python tools/coreaudio_volume_spike.py --test-id 147
        Same round-trip test, targeting an exact device ID instead of a
        name — needed when two devices share an identical name (e.g. a
        USB Audio Class device that splits playback/record into separate
        CoreAudio objects with the same "USB Audio CODEC" name — check
        the printed uid: line, which includes the shared USB serial
        number, to confirm they're the same physical hardware before
        picking the playback-side ID).
"""

import argparse
import ctypes
import ctypes.util
import struct
import sys

# ---------------------------------------------------------------------------
# CoreAudio / CoreFoundation ctypes bindings — just enough of the HAL's
# AudioObject property API to enumerate devices and get/set/query one
# property (kAudioDevicePropertyVolumeScalar).
# ---------------------------------------------------------------------------

_ca_path = ctypes.util.find_library("CoreAudio")
_cf_path = ctypes.util.find_library("CoreFoundation")
if not _ca_path or not _cf_path:
    sys.exit("CoreAudio/CoreFoundation not found — this script is macOS-only.")

CA = ctypes.CDLL(_ca_path)
CF = ctypes.CDLL(_cf_path)

AudioObjectID = ctypes.c_uint32
OSStatus = ctypes.c_int32
CFStringRef = ctypes.c_void_p
CFIndex = ctypes.c_long
Boolean = ctypes.c_uint8

kAudioObjectSystemObject = AudioObjectID(1)
kCFStringEncodingUTF8 = 0x08000100


def fourcc(code: str) -> int:
    """Pack a 4-char code the way CoreAudio's C headers do (e.g. 'volm')."""
    return struct.unpack(">I", code.encode("ascii"))[0]


kAudioObjectPropertyScopeGlobal = fourcc("glob")
kAudioObjectPropertyScopeOutput = fourcc("outp")
kAudioObjectPropertyScopeInput = fourcc("inpu")
kAudioObjectPropertyElementMain = 0
kAudioHardwarePropertyDevices = fourcc("dev#")
kAudioObjectPropertyName = fourcc("lnam")
kAudioDevicePropertyDeviceUID = fourcc("uid ")
kAudioDevicePropertyVolumeScalar = fourcc("volm")

# Probe the master element (0) plus the first two channels (covers the
# overwhelming majority of USB audio codecs, which are mono or stereo) —
# some devices only expose per-channel volume with no master element.
PROBE_ELEMENTS = [0, 1, 2]


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


CA.AudioObjectGetPropertyDataSize.argtypes = [
    AudioObjectID, ctypes.POINTER(AudioObjectPropertyAddress),
    ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
CA.AudioObjectGetPropertyDataSize.restype = OSStatus

CA.AudioObjectGetPropertyData.argtypes = [
    AudioObjectID, ctypes.POINTER(AudioObjectPropertyAddress),
    ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
CA.AudioObjectGetPropertyData.restype = OSStatus

CA.AudioObjectSetPropertyData.argtypes = [
    AudioObjectID, ctypes.POINTER(AudioObjectPropertyAddress),
    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
CA.AudioObjectSetPropertyData.restype = OSStatus

CA.AudioObjectHasProperty.argtypes = [
    AudioObjectID, ctypes.POINTER(AudioObjectPropertyAddress)]
CA.AudioObjectHasProperty.restype = Boolean

CA.AudioObjectIsPropertySettable.argtypes = [
    AudioObjectID, ctypes.POINTER(AudioObjectPropertyAddress), ctypes.POINTER(Boolean)]
CA.AudioObjectIsPropertySettable.restype = OSStatus

CF.CFStringGetLength.argtypes = [CFStringRef]
CF.CFStringGetLength.restype = CFIndex
CF.CFStringGetMaximumSizeForEncoding.argtypes = [CFIndex, ctypes.c_uint32]
CF.CFStringGetMaximumSizeForEncoding.restype = CFIndex
CF.CFStringGetCString.argtypes = [CFStringRef, ctypes.c_char_p, CFIndex, ctypes.c_uint32]
CF.CFStringGetCString.restype = Boolean
CF.CFRelease.argtypes = [ctypes.c_void_p]


def cfstring_to_str(cfstr_ptr) -> str:
    if not cfstr_ptr:
        return ""
    length = CF.CFStringGetLength(cfstr_ptr)
    max_size = CF.CFStringGetMaximumSizeForEncoding(length, kCFStringEncodingUTF8) + 1
    buf = ctypes.create_string_buffer(max_size)
    ok = CF.CFStringGetCString(cfstr_ptr, buf, max_size, kCFStringEncodingUTF8)
    return buf.value.decode("utf-8") if ok else ""


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

def get_device_list() -> list[int]:
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain)
    size = ctypes.c_uint32(0)
    status = CA.AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size))
    if status != 0:
        raise RuntimeError(f"AudioObjectGetPropertyDataSize(devices) failed: {status}")
    count = size.value // ctypes.sizeof(AudioObjectID)
    arr = (AudioObjectID * count)()
    status = CA.AudioObjectGetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(addr), 0, None, ctypes.byref(size), arr)
    if status != 0:
        raise RuntimeError(f"AudioObjectGetPropertyData(devices) failed: {status}")
    return list(arr)


def get_device_name(device_id: int) -> str:
    addr = AudioObjectPropertyAddress(
        kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain)
    size = ctypes.c_uint32(ctypes.sizeof(CFStringRef))
    cfstr = CFStringRef()
    status = CA.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(cfstr))
    if status != 0 or not cfstr:
        return "<unnamed>"
    name = cfstring_to_str(cfstr)
    CF.CFRelease(cfstr)
    return name


def get_device_uid(device_id: int) -> str:
    """Encodes USB vendor/product/serial info where available — the
    reliable way to tell apart two devices that share a generic name like
    'USB Audio CODEC' (common: some USB Audio Class chips split
    record/playback into separate CoreAudio device objects)."""
    addr = AudioObjectPropertyAddress(
        kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain)
    size = ctypes.c_uint32(ctypes.sizeof(CFStringRef))
    cfstr = CFStringRef()
    status = CA.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(cfstr))
    if status != 0 or not cfstr:
        return "<no uid>"
    uid = cfstring_to_str(cfstr)
    CF.CFRelease(cfstr)
    return uid


def probe_volume(device_id: int, scope: int, element: int):
    """Read-only: does this (device, scope, element) have a volume
    property, and does the HAL report it as settable? Makes no Set call."""
    addr = AudioObjectPropertyAddress(kAudioDevicePropertyVolumeScalar, scope, element)
    if not CA.AudioObjectHasProperty(device_id, ctypes.byref(addr)):
        return None
    settable = Boolean(0)
    status = CA.AudioObjectIsPropertySettable(device_id, ctypes.byref(addr), ctypes.byref(settable))
    if status != 0:
        return None
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_float))
    value = ctypes.c_float()
    status = CA.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value))
    current = round(value.value, 3) if status == 0 else None
    return {"settable": bool(settable.value), "current": current}


def roundtrip_test(device_id: int, scope: int, element: int, current_value: float):
    """Mutating: set a nudged test value, read it back, then restore the
    original — proves settability end-to-end rather than trusting the
    HAL's IsPropertySettable flag alone."""
    addr = AudioObjectPropertyAddress(kAudioDevicePropertyVolumeScalar, scope, element)
    test_value = 0.5 if abs(current_value - 0.5) > 0.05 else 0.4

    new_val = ctypes.c_float(test_value)
    status = CA.AudioObjectSetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.sizeof(new_val), ctypes.byref(new_val))
    if status != 0:
        return False, f"Set failed: OSStatus {status}"

    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_float))
    readback = ctypes.c_float()
    CA.AudioObjectGetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(readback))

    # Restore original value regardless of outcome.
    restore_val = ctypes.c_float(current_value)
    CA.AudioObjectSetPropertyData(
        device_id, ctypes.byref(addr), 0, None, ctypes.sizeof(restore_val), ctypes.byref(restore_val))

    if abs(readback.value - test_value) < 0.02:
        return True, f"set {test_value:.2f} -> read back {readback.value:.2f} -> restored {current_value:.2f}"
    return False, f"set {test_value:.2f} but read back {readback.value:.2f} (didn't take)"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", metavar="NAME_SUBSTRING", default=None,
                         help="Device name substring (case-insensitive) to round-trip test after listing.")
    parser.add_argument("--test-id", metavar="DEVICE_ID", type=int, default=None,
                         help="Exact device ID to round-trip test — use when two devices share a name.")
    args = parser.parse_args()

    devices = get_device_list()
    print(f"Found {len(devices)} CoreAudio device(s):\n")

    matches = []
    for device_id in devices:
        name = get_device_name(device_id)
        uid = get_device_uid(device_id)
        print(f"[{device_id}] {name}  (uid: {uid})")
        if args.test_id == device_id:
            matches = [(device_id, name)]

        for scope_name, scope in (("output", kAudioObjectPropertyScopeOutput),
                                   ("input", kAudioObjectPropertyScopeInput)):
            found_any = False
            for element in PROBE_ELEMENTS:
                result = probe_volume(device_id, scope, element)
                if result is None:
                    continue
                found_any = True
                tag = "element 0 (master)" if element == 0 else f"channel {element}"
                settable_str = "SETTABLE" if result["settable"] else "fixed/not settable"
                print(f"    {scope_name} {tag}: volume={result['current']}  {settable_str}")
            if not found_any:
                print(f"    {scope_name}: no volume property on any probed element")

        if args.test and args.test.lower() in name.lower():
            matches.append((device_id, name))
        print()

    if not args.test and args.test_id is None:
        print("Run again with --test \"<name substring>\" or --test-id <id> to round-trip "
              "test a specific device's output volume once you've identified the FT-991A's "
              "audio device above.")
        return

    if args.test_id is not None and not matches:
        print(f"--test-id {args.test_id} did not match any listed device ID.")
        return

    if len(matches) != 1:
        selector = args.test if args.test else f"id {args.test_id}"
        print(f"'{selector}' matched {len(matches)} device(s) "
              f"({[n for _, n in matches]}) — need exactly 1 to run the round-trip test safely. Aborting.")
        return

    device_id, name = matches[0]
    print(f"--- Round-trip test on [{device_id}] {name} (output scope) ---")
    master = probe_volume(device_id, kAudioObjectPropertyScopeOutput, 0)
    if master is not None and master["settable"]:
        ok, detail = roundtrip_test(device_id, kAudioObjectPropertyScopeOutput, 0, master["current"])
        print(f"  master: {'PASS' if ok else 'FAIL'} — {detail}")
    else:
        print("  master: no settable master volume — trying per-channel")
        for element in (1, 2):
            result = probe_volume(device_id, kAudioObjectPropertyScopeOutput, element)
            if result is not None and result["settable"]:
                ok, detail = roundtrip_test(device_id, kAudioObjectPropertyScopeOutput, element, result["current"])
                print(f"  channel {element}: {'PASS' if ok else 'FAIL'} — {detail}")
            else:
                print(f"  channel {element}: not settable or no volume property")

    print("\nBottom line: if any line above says PASS, an OS-level volume knob "
          "on this device is viable (Phase 5 of the TX-power-target plan is in "
          "scope). If nothing passed, this device has no settable volume — "
          "the closed loop ships ceiling-only.")


if __name__ == "__main__":
    main()
