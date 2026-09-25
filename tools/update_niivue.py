"""Vendor NiiVue for the browser review page, as one self-contained ES module.

    python tools/update_niivue.py 0.69.0

NiiVue's npm package ships ``build/index.min.js``, which holds the whole library -- its
dependencies included -- as one URL-encoded module, ``export const esm = "..."``. This
script downloads the package from the npm registry, checks it against the registry's
checksum, decodes that module, checks that it imports nothing, and writes it to
``qc_server/static/vendor/niivue-<version>.min.js``.

After an update, point the import at the top of ``static/review.js`` at the new file,
delete the old one, and run the test suite, which checks the two agree.

Standard library only, so it runs in any Python 3.10+.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

REGISTRY = "https://registry.npmjs.org/@niivue/niivue"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "qc_server" / "static" / "vendor"
BUNDLE_MEMBER = "package/build/index.min.js"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def verify(data: bytes, integrity: str) -> None:
    """Check the tarball against npm's ``sha512-<base64>`` integrity string."""
    algorithm, _, expected = integrity.partition("-")
    if algorithm != "sha512":
        raise SystemExit(f"Unexpected integrity algorithm '{algorithm}'.")
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if actual != expected:
        raise SystemExit("The downloaded package does not match the registry's checksum.")


def decode_bundle(source: str) -> str:
    match = re.fullmatch(r'\s*export const esm\s*=\s*"(.*)";?\s*', source, re.S)
    if match is None:
        raise SystemExit(f"{BUNDLE_MEMBER} is not the encoded module this script expects; check the package layout.")
    code = urllib.parse.unquote(match.group(1))
    # The page serves this one file and nothing else, so it must not import anything.
    imports = re.findall(r'(?:^|[;}\n])\s*import\s*(?:[\w*{][^;]*?\bfrom\s*)?["\']([^"\']+)["\']', code)
    imports += re.findall(r'\bimport\(\s*["\']([^"\']+)["\']', code)
    if imports:
        raise SystemExit(f"The bundle imports other modules, which the page cannot serve: {sorted(set(imports))}")
    if not re.search(r"export\s*\{[^}]*\bNiivue\b", code[-5000:]):
        raise SystemExit("The bundle does not export Niivue.")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="the NiiVue version to vendor, e.g. 0.69.0")
    args = parser.parse_args(argv)

    meta = json.loads(fetch(f"{REGISTRY}/{args.version}"))
    tarball = fetch(meta["dist"]["tarball"])
    verify(tarball, meta["dist"]["integrity"])
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as package:
        member = package.extractfile(BUNDLE_MEMBER)
        if member is None:
            raise SystemExit(f"The package has no {BUNDLE_MEMBER}.")
        code = decode_bundle(member.read().decode("utf-8"))

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    target = VENDOR_DIR / f"niivue-{meta['version']}.min.js"
    target.write_text(code, encoding="utf-8", newline="\n")
    print(f"Wrote {target} ({len(code.encode('utf-8')) / 1e6:.1f} MB), NiiVue {meta['version']}, {meta.get('license')}.")
    print("Now update the import at the top of static/review.js and remove the previous version.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
