def load_settings(path):
    """Read `key = value` lines from `path` into a dict."""
    settings = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            key, sep, value = line.partition("=")
            if sep:
                settings[key.strip()] = value.strip()
    return settings
