
import json
import math
import os
import tempfile
import fibre.libfibre
import odrive
from odrive.utils import OperationAbortedException, yes_no_prompt

def obj_to_path(root, obj):
    for k in dir(root):
        if k.startswith('__'):
            continue
        if k.startswith('_') and not k.endswith('_property'):
            continue
        try:
            v = getattr(root, k, None)
        except Exception:
            continue
        if isinstance(v, fibre.libfibre.RemoteObject):
            if v == obj:
                return k
            subpath = obj_to_path(v, obj)
            if not subpath is None:
                return k + "." + subpath
    return None

def path_to_obj(root, path):
    if not path or not isinstance(path, str):
        return None
    cur = root
    for seg in path.split('.'):
        cur = getattr(cur, seg, None)
        if cur is None:
            return None
    return cur

def get_dict(root, obj, is_config_object):
    result = {}

    for k in dir(obj):
        v = getattr(obj, k)
        if k.startswith('_') and k.endswith('_property') and is_config_object:
            if not hasattr(v, 'exchange'):
                continue
            val = v.read()
            if isinstance(val, fibre.libfibre.RemoteObject):
                val = obj_to_path(root, val)
            result[k[1:-9]] = val
        elif not k.startswith('_') and isinstance(v, fibre.libfibre.RemoteObject):
            sub_dict = get_dict(root, v, (k == 'config') or is_config_object)
            if sub_dict != {}:
                result[k] = sub_dict

    return result

def set_dict(obj, path, config_dict, root=None):
    if root is None:
        root = obj
    errors = []
    for (k,v) in config_dict.items():
        name = path + ("." if path != "" else "") + k
        if not k in dir(obj):
            errors.append("Could not restore {}: property not found on device".format(name))
            continue
        if isinstance(v, dict):
            errors += set_dict(getattr(obj, k), name, v, root)
        else:
            prop_name = '_' + k + '_property'
            if not hasattr(obj, prop_name):
                errors.append("Could not restore {}: property not found on device".format(name))
                continue
            remote_attribute = getattr(obj, prop_name)
            if not hasattr(remote_attribute, 'exchange'):
                continue
            try:
                if isinstance(v, str) and v in ("Infinity", "+Infinity", "-Infinity", "NaN", "inf", "+inf", "-inf", "nan"):
                    v = float(v)
                elif isinstance(v, str):
                    v_obj = path_to_obj(root, v)
                    if v_obj is None and v != "":
                        errors.append("Could not restore {}: endpoint {} not found on device".format(name, v))
                        continue
                    v = v_obj
                elif v is None:
                    intf_name = getattr(remote_attribute, '_intf_name', '') or type(remote_attribute).__name__
                    if 'object_ref' not in intf_name:
                        continue
                remote_attribute.exchange(v)
            except Exception as ex:
                errors.append("Could not restore {}: {}".format(name, str(ex)))
    return errors

def get_temp_config_filename(device):
    serial_number = odrive.get_serial_number_str_sync(device)
    safe_serial_number = ''.join(filter(str.isalnum, serial_number))
    return os.path.join(tempfile.gettempdir(), 'odrive-config-{}.json'.format(safe_serial_number))

def _sanitize_for_json(obj):
    """Recursively replace non-finite floats (inf/-inf/nan) with JSON-compliant
    strings so the exported file is valid JSON and preserves infinite bounds."""
    if isinstance(obj, float) and not math.isfinite(obj):
        if math.isnan(obj):
            return "NaN"
        return "Infinity" if obj > 0 else "-Infinity"
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def backup_config(device, filename, logger):
    """
    Exports the configuration of an ODrive to a JSON file.
    If no file name is provided, the file is placed into a
    temporary directory.
    """

    if filename is None:
        filename = get_temp_config_filename(device)

    logger.info("Saving configuration to {}...".format(filename))

    if os.path.exists(filename):
        if not yes_no_prompt("The file {} already exists. Do you want to override it?".format(filename), True):
            raise OperationAbortedException()

    data = get_dict(device, device, False)
    with open(filename, 'w') as file:
        json.dump(_sanitize_for_json(data), file)
    logger.info("Configuration saved.")

def restore_config(device, filename, logger):
    """
    Restores the configuration stored in a file 
    """

    if filename is None:
        filename = get_temp_config_filename(device)

    with open(filename) as file:
        data = json.load(file)

    logger.info("Restoring configuration from {}...".format(filename))
    errors = set_dict(device, "", data)

    for error in errors:
        logger.info(error)
    if errors:
        logger.warn("Some of the configuration could not be restored.")
    
    try:
        device.save_configuration()
    except fibre.libfibre.ObjectLostError:
        pass # Saving configuration makes the device reboot
    logger.info("Configuration restored.")
