#!/usr/bin/env python3
"""Parse the page's JavaScript before shipping it.

A syntax error there is silent in the worst way: the HTML and the logo still
render, so the page looks alive while nothing ever loads.  That cost an
afternoon once — a `\n` inside the page template, which Python turned into a
real newline in the middle of a JavaScript string, because the template was not
a raw string.
"""
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "addon"))
import webui  # noqa: E402

script = re.search(r"<script>(.*?)</script>", webui.PAGE, re.S)
if not script:
    sys.exit("no <script> block found in the page")
tmp = pathlib.Path("/tmp/thbr_webui_check.js")
tmp.write_text(script.group(1))
node = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
if node.returncode != 0:
    sys.exit("the page's JavaScript does not parse:\n" + node.stderr)
if "\n" in re.sub(r"\\n", "", "".join(re.findall(r"'[^'\n]*'", script.group(1)))):
    sys.exit("a string literal spans a line break")
print(f"page javascript parses ({len(script.group(1))} characters)")
