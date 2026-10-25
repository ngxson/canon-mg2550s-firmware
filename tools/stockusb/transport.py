"""Direct libusb transport; no flashing, device resets or configuration changes."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time


class ProtocolError(RuntimeError):
    pass


class Trace:
    """Metadata only: never record image, raster or document bytes."""
    def __init__(self, path=None):
        self.file = open(path, 'x', encoding='utf8') if path else None
        self.lock = threading.Lock()

    def __call__(self, event, **fields):
        if self.file:
            row = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   'monotonic_ns': time.monotonic_ns(), 'event': event, **fields}
            with self.lock:
                self.file.write(json.dumps(row)+'\n'); self.file.flush()

    def close(self):
        if self.file: self.file.close()


class USB:
    def __init__(self, role, trace=None):
        import usb.core
        import usb.util
        self.core, self.util = usb.core, usb.util
        self.trace = trace or (lambda *a, **kw: None)
        self.dev = None
        self.claimed = False
        self.lock_fd = None
        self.interface = {'scan': 0, 'print': 1}[role]
        devices = list(usb.core.find(find_all=True, idVendor=0x04a9, idProduct=0x176d))
        if len(devices) != 1:
            raise RuntimeError(f'Expected one USB-attached MG2500; found {len(devices)}. '
                               'Connect the printer USB cable to this computer, not the ESP32 host.')
        self.dev = devices[0]
        try:
            product = usb.util.get_string(self.dev, self.dev.iProduct)
            if product != 'MG2500 series':
                raise RuntimeError(f'Expected stock MG2500 runtime, found {product!r}')
            # Print and scan own different interfaces, but must not run as
            # simultaneous jobs. Telemetry deliberately does not take this lock.
            lock=Path(tempfile.gettempdir())/f'mg2500-job-{os.getuid()}-{self.dev.bus}-{self.dev.address}.lock'
            self.lock_fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            try: fcntl.flock(self.lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as e: raise RuntimeError('Another Python print/scan job owns this printer') from e
            cfg = self.dev.get_active_configuration()
            intf = cfg[(self.interface, 0)]
            expected = (255, 0, 255) if role == 'scan' else (7, 1, 2)
            if (intf.bInterfaceClass,intf.bInterfaceSubClass,intf.bInterfaceProtocol) != expected:
                raise RuntimeError('Unexpected stock interface descriptors')
            bulk = [ep for ep in intf if ep.bmAttributes & 3 == 2]
            inputs = [ep for ep in bulk if ep.bEndpointAddress & 0x80]
            outputs = [ep for ep in bulk if not ep.bEndpointAddress & 0x80]
            if len(inputs) != 1 or len(outputs) != 1:
                raise RuntimeError('Expected one bulk IN/OUT pair')
            self.input, self.output = inputs[0], outputs[0]
            usb.util.claim_interface(self.dev,self.interface)
            self.claimed = True
            self.trace('usb_open', role=role, vid='04a9', pid='176d',
                       bcd_device=self.dev.bcdDevice, interface=self.interface,
                       input=self.input.bEndpointAddress, output=self.output.bEndpointAddress,
                       packet_size=self.input.wMaxPacketSize)
        except BaseException:
            self.close()
            raise

    def write(self, data, timeout=1000):
        start=time.monotonic_ns()
        try:
            n=self.dev.write(self.output.bEndpointAddress,data,timeout=timeout)
        except self.core.USBTimeoutError as e:
            # Never replay a timed-out OUT: an unknown prefix may have arrived.
            raise TimeoutError('USB OUT timed out; transfer was not resent') from e
        if n != len(data): raise ProtocolError(f'Short USB OUT: {n}/{len(data)}; not resent')
        self.trace('usb_out',bytes=n,elapsed_ns=time.monotonic_ns()-start)
        return n

    def write_stalling(self, data, deadline, on_stall=None, slice_ms=1000, stall_report=15.0):
        """Send all of `data`, waiting while the printer NAKs.

        A printing or paper-less MG2500 refuses bulk OUT for as long as the job
        takes; the ESP32 host waits indefinitely and logs port status every
        15 s. Here the wait is bounded by `deadline` (time.monotonic()). Each
        libusb slice reports how many bytes it transferred before timing out,
        so progress is kept exactly and nothing is ever resent. `on_stall`
        receives (elapsed_seconds, port_status_or_None) every `stall_report`
        seconds. Returns the number of bytes sent, equal to len(data).
        """
        data=bytes(data)
        bulk=self._bulk_transfer()
        if bulk is None:
            # Non-libusb backend: one long transfer, same "never resend" rule.
            return self.write(data, max(1000, int((deadline-time.monotonic())*1000)))
        import ctypes
        sent=0; start=time.monotonic(); last_report=start; waited=0
        while sent<len(data):
            now=time.monotonic()
            if now>=deadline:
                raise TimeoutError(f'Printer refused data for {now-start:.0f}s; '
                                   f'{sent}/{len(data)} bytes sent, nothing resent')
            remaining=min(slice_ms, max(1, int((deadline-now)*1000)))
            buf=(ctypes.c_ubyte*(len(data)-sent)).from_buffer_copy(data[sent:])
            moved=ctypes.c_int(0)
            t0=time.monotonic_ns()
            ret=bulk(self.output.bEndpointAddress, buf, len(buf), ctypes.byref(moved), remaining)
            elapsed=time.monotonic_ns()-t0
            if moved.value<0 or moved.value>len(buf):
                raise ProtocolError('libusb reported an impossible transfer length')
            sent+=moved.value
            if ret==0:
                if moved.value: self.trace('usb_out',bytes=moved.value,elapsed_ns=elapsed,waited_ms=waited)
                waited=0
                continue
            if ret!=-7:   # LIBUSB_ERROR_TIMEOUT is the only tolerated outcome.
                raise ProtocolError(f'USB OUT failed (libusb {ret}) after {sent}/{len(data)} bytes; not resent')
            if moved.value: self.trace('usb_out',bytes=moved.value,elapsed_ns=elapsed,partial=True,waited_ms=waited)
            waited+=remaining
            if time.monotonic()-last_report>=stall_report:
                last_report=time.monotonic()
                status=self.port_status()
                self.trace('usb_out_stall',seconds=round(last_report-start,1),sent=sent,of=len(data),port_status=status)
                if on_stall: on_stall(last_report-start,status)
        return sent

    def _bulk_transfer(self):
        """libusb_bulk_transfer with exact `transferred` reporting, or None."""
        try:
            ctx=self.dev._ctx
            backend=ctx.backend
            if type(backend).__module__!='usb.backend.libusb1': return None
            ctx.managed_open()
            handle=ctx.handle.handle
            lib=backend.lib
        except Exception:
            return None
        return lambda ep,buf,n,moved,timeout: lib.libusb_bulk_transfer(handle,ep,buf,n,moved,timeout)

    def port_status(self):
        """Printer-class GET_PORT_STATUS byte (0x20 paper empty, 0x10 selected, 0x08 no error), or None."""
        if self.interface!=1: return None
        try: return int(self.dev.ctrl_transfer(0xa1,1,0,self.interface,1,timeout=2000)[0])
        except Exception: return None

    def read(self, length, timeout=1000):
        # Match the ESP32's packet-aligned requests, also at USB high speed.
        packet=self.input.wMaxPacketSize
        request=(length+packet-1)//packet*packet
        start=time.monotonic_ns()
        try:
            data=bytes(self.dev.read(self.input.bEndpointAddress,request,timeout=timeout))
        except self.core.USBTimeoutError as e:
            raise TimeoutError('USB IN timed out') from e
        if len(data)>length: raise ProtocolError('Reply exceeds requested protocol length')
        self.trace('usb_in',bytes=len(data),elapsed_ns=time.monotonic_ns()-start)
        return data

    def soft_reset_print(self):
        """Printer-class session cancellation, never a device/firmware reboot."""
        if self.interface != 1: raise RuntimeError('Print-only cancellation')
        self.dev.ctrl_transfer(0x21,2,0,self.interface,b'',timeout=2000)
        self.trace('printer_class_soft_reset')

    def close(self):
        try:
            if self.dev is not None:
                try:
                    if self.claimed: self.util.release_interface(self.dev,self.interface)
                finally:
                    self.util.dispose_resources(self.dev)
                    self.dev=None; self.claimed=False
        finally:
            if self.lock_fd is not None:
                os.close(self.lock_fd);self.lock_fd=None

    def __enter__(self): return self
    def __exit__(self,*exc): self.close()
