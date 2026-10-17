from app.settings.loader import load_settings


def main(path="settings.ini"):
    settings = load_settings(path)
    print(f"loaded {len(settings)} settings")
