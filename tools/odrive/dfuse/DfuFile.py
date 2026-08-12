import argparse
import struct


def named(tuple, names):
    return dict(zip(names, tuple, strict=False))


def parse(fmt, data, names):
    return named(struct.unpack(fmt, data), names)


def fileunpack(f, fmt, names):
    n = struct.calcsize(fmt)
    return parse(fmt, f.read(n), names)


class DfuFile:
    def __init__(self, path):
        self.targets = []
        self.devInfo = {}

        try:
            dfufile = open(path, "rb")
        except OSError:
            raise argparse.ArgumentTypeError(f"Could not open file {path!r}") from None

        with dfufile:
            header = fileunpack(dfufile, "<5sBLB", ("signature", "version", "size", "targets"))

            if header["signature"] != b"DfuSe":
                raise argparse.ArgumentTypeError("File signature does not match")
            if header["version"] != 1:
                raise argparse.ArgumentTypeError("Unsupport DfuSe file version")

            for _t in range(header["targets"]):
                target_prefix = fileunpack(
                    dfufile, "<6sBL255sLL", ("signature", "alternate", "named", "name", "size", "elements")
                )
                if target_prefix["signature"] != b"Target":
                    raise argparse.ArgumentTypeError("Target signature does not match")

                target = {
                    "name": target_prefix["name"].decode("ascii").rstrip("\0"),
                    "alternate": target_prefix["alternate"],
                    "elements": [],
                }

                for _e in range(target_prefix["elements"]):
                    element_prefix = fileunpack(dfufile, "<LL", ("address", "size"))
                    element = {"address": element_prefix["address"], "data": dfufile.read(element_prefix["size"])}
                    target["elements"].append(element)

                self.targets.append(target)

            suffix = fileunpack(
                dfufile, "<HHHH3sBL", ("fwVersion", "pid", "vid", "dfuSpec", "signature", "length", "crc")
            )
            if suffix["signature"] != b"UFD":
                raise argparse.ArgumentTypeError("File's suffix signature does not match")

            self.devInfo = dict(suffix)
            del self.devInfo["signature"]
            del self.devInfo["length"]
            del self.devInfo["crc"]
