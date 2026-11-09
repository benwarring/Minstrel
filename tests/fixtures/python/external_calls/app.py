import json
import os.path


def main():
    path = os.path.join("data", "config.json")
    print(json.dumps({"path": path}))
