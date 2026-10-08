"""Expose synthetic CI test failures in job annotations, without full tracebacks."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def main():
    path = Path(sys.argv[1])
    if not path.is_file():
        print("::error::Pytest failed without a JUnit report")
        return
    for case in ET.parse(path).getroot().iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        detail = problem.get("message", "Test failed").splitlines()[0][:600]
        message = f"{case.get('classname', '')}.{case.get('name', '')}: {detail}"
        message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error::" + message)


if __name__ == "__main__":
    main()
