"""YAML-backed configuration for the on-screen keyboard.

Two pieces:

* `ObjectConfig`  a named bag of decoded YAML sections. Subclasses (see
  `vkb.VirtualKeyboardConfig`) read `self.objects` in `construct()` and turn it
  into live layout objects.
* `YamlFile`  one layout file on disk, located through `adusk.resources`, read
  once, then fed section-by-section into an `ObjectConfig`.

Typical use, from `adusk.py`:

    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("pages", kb_config)
    layout.add_to_config("keys", kb_config)
    kb_config.construct()
"""

import yaml

from adusk import resources

# Parse layouts with libyaml (PyYAML's C loader) when the extension is present.
# The layout files are re-read on every OSK open, and the pure-Python loader is
# ~10x slower on them  measured 86 ms (classic) / 124 ms (phone) against
# 9 ms / 14 ms  which is dead time on the open the user is waiting through.
# Same safe schema either way; a build without the extension keeps working on
# the pure-Python loader.
_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class ConfigError(Exception):
    """A layout file is missing, unreadable, or missing a required section."""


class ObjectConfig:
    """Named YAML sections awaiting `construct()`."""

    def __init__(self):
        # Per-instance, NOT class-level. The OSK is opened and closed many
        # times in one process; a dict shared across every instance let a
        # section read by an earlier layout (e.g. the phone layout's `pages`)
        # stay visible to a later config that never set it, so switching back
        # to the classic layout kept rebuilding the phone one.
        self.objects = {}

    def set_object(self, name, data):
        self.objects[name] = data

    def construct(self):
        """Build live objects from `self.objects`. Overridden by subclasses."""


class YamlFile:
    """One YAML layout file, resolved under `data/cfg/`."""

    def __init__(self, filename):
        self.filename = filename
        self.file_path = resources.find_cfg_resource(filename)
        if self.file_path is None:
            raise ConfigError(
                "Could not find layout file '{}' under any data/cfg "
                "directory".format(filename))
        self.yaml_data = {}

    def read(self):
        with open(self.file_path, "r", encoding="utf-8") as handle:
            self.yaml_data = yaml.load(handle, Loader=_SAFE_LOADER) or {}

    def add_to_config(self, key, object_config):
        """Copy one top-level section into `object_config`."""
        if key not in self.yaml_data:
            raise ConfigError(
                "{} is malformed: no top-level '{}' section".format(
                    self.filename, key))
        object_config.set_object(key, self.yaml_data[key])
