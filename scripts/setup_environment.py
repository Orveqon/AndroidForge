#!/usr/bin/env python3
"""AndroidForge — Toolchain Setup.

Reads the JSON produced by detect_project.py and config/toolchain-rules.yaml,
then decides:
  • Which JDK version to install
  • Which Gradle version to install (if no usable wrapper)
  • Which AGP version is in use
  • Which NDK version to install (if any)
  • Whether Flutter is needed
  • Which compatibility fixes (legacy_fixes) to apply

The script writes its decisions to $GITHUB_OUTPUT and prints a JSON summary.
It applies compatibility fixes to the build workspace, not the original ZIP.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML not installed. Run: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "config" / "toolchain-rules.yaml"


def load_rules(path: Path | None = None) -> dict[str, Any]:
    p = path or RULES_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data or {}


def major(v: str) -> int:
    try:
        return int(str(v).split(".")[0])
    except (ValueError, IndexError):
        return 0


def minor(v: str) -> int:
    try:
        return int(str(v).split(".")[1])
    except (ValueError, IndexError):
        return 0


def pick_jdk_for_agp(agp: str | None, rules: dict) -> str:
    """Determine the JDK version required by AGP."""
    if not agp:
        return "17"

    m = major(agp)

    if m >= 8:
        return "17"
    if m == 7:
        return "11"
    if m == 4:
        return "11"
    if m <= 3:
        return "8"

    for pattern, jdk in rules.get("agp_to_jdk", {}).items():
        if pattern.endswith(".x"):
            pm = pattern[:-2]
            if str(agp).startswith(pm):
                return str(jdk)
        elif str(agp).startswith(pattern):
            return str(jdk)

    return "17"


def pick_jdk_for_gradle(
    gradle: str | None,
    rules: dict,
) -> str:
    if not gradle:
        return "17"

    m = major(gradle)

    if m >= 8:
        return "17"
    if m == 7:
        return "11"
    if m <= 6:
        return "8"

    return "17"


def pick_gradle_for_agp(
    agp: str | None,
    rules: dict,
) -> str | None:
    """Look up the minimum Gradle version required by AGP."""
    if not agp:
        return None

    mapping = rules.get("agp_to_gradle", {})

    if agp in mapping:
        return mapping[agp]

    parts = str(agp).split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in mapping:
            return mapping[prefix]

    m = major(agp)
    for key, value in mapping.items():
        if major(key) == m:
            return value

    return None


def pick_flutter_version(
    detect: dict,
    rules: dict,
) -> str:
    flutter = detect.get("flutter", {}) or {}
    constraint = flutter.get("flutter_version_constraint")

    if constraint:
        match = re.search(r"(\d+\.\d+\.\d+)", constraint)
        if match:
            return match.group(1)

    return str(
        rules.get("flutter", {}).get("default_version", "3.24.0")
    )


def pick_ndk_version(
    detect: dict,
    rules: dict,
) -> str:
    ndk_version = (detect.get("versions") or {}).get("ndk_version")

    if ndk_version:
        return str(ndk_version)

    return str(
        rules.get("ndk", {}).get("default_version", "26.1.10909125")
    )


def determine_legacy_fixes(
    detect: dict,
    rules: dict,
) -> list[dict]:
    fixes: list[dict] = []

    wrapper = detect.get("wrapper", {}) or {}
    gradle_version = wrapper.get("version")
    has_local = "local.properties" in detect.get(
        "indicator_files", {}
    )

    if wrapper.get("missing_jar") or not wrapper.get("present"):
        fixes.append({
            "name": "patch_gradle_wrapper",
            "reason": "Missing or corrupt gradle-wrapper.jar",
        })

    if wrapper.get("uses_http"):
        fixes.append({
            "name": "migrate_https",
            "reason": "Wrapper uses http:// distribution URL",
        })

    if gradle_version and major(gradle_version) < 4:
        fixes.append({
            "name": "disable_gradle_daemon",
            "reason": f"Very old Gradle: {gradle_version}",
        })

    if (
        not has_local
        and detect.get("project_type") in ("gradle", "flutter")
    ):
        fixes.append({
            "name": "inject_local_properties",
            "reason": "local.properties missing",
        })

    return fixes


def apply_fixes(
    detect: dict,
    fixes: list[dict],
    android_sdk_root: str,
) -> list[str]:
    """Apply compatibility fixes to the project tree."""
    applied: list[str] = []
    root = Path(detect["project_root"])
    wrapper_properties = (
        root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    )

    for fix in fixes:
        name = fix["name"]

        if name == "migrate_https" and wrapper_properties.exists():
            content = wrapper_properties.read_text(
                encoding="utf-8",
                errors="replace",
            )
            updated = re.sub(
                r"distributionUrl=http://",
                "distributionUrl=https://",
                content,
            )

            if updated != content:
                wrapper_properties.write_text(
                    updated,
                    encoding="utf-8",
                )
                applied.append(
                    "Migrated wrapper distribution URL to HTTPS "
                    f"in {wrapper_properties}"
                )

        elif name == "inject_local_properties":
            (root / "local.properties").write_text(
                f"sdk.dir={android_sdk_root}\n",
                encoding="utf-8",
            )
            applied.append(
                f"Created local.properties with sdk.dir={android_sdk_root}"
            )

        elif name == "disable_gradle_daemon":
            gradle_properties = root / "gradle.properties"
            content = (
                gradle_properties.read_text(encoding="utf-8")
                if gradle_properties.exists()
                else ""
            )

            if "org.gradle.daemon" not in content:
                with gradle_properties.open(
                    "a",
                    encoding="utf-8",
                ) as file:
                    file.write(
                        "\n# AndroidForge: disable Gradle daemon "
                        "for old Gradle\n"
                        "org.gradle.daemon=false\n"
                    )

                applied.append(
                    "Disabled Gradle daemon via gradle.properties"
                )

    # ---- Configure Gradle heap and AndroidX ----
    heap_line = (
        "org.gradle.jvmargs=-Xmx4g "
        "-XX:+UseG1GC -Dfile.encoding=UTF-8"
    )

    androidx_lines = [
        "android.useAndroidX=true",
        "android.enableJetifier=true",
    ]

    def inject_gradle_properties(
        props_path: Path,
        location_label: str,
    ) -> None:
        if props_path.exists():
            content = props_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            changes: list[str] = []

            match = re.search(
                r"^org\.gradle\.jvmargs\s*=\s*(.+)$",
                content,
                re.MULTILINE,
            )

            if match:
                existing = match.group(1).strip()
                heap_match = re.search(
                    r"-Xmx(\d+)([gm])",
                    existing,
                    re.IGNORECASE,
                )

                if heap_match:
                    size = int(heap_match.group(1))
                    unit = heap_match.group(2).lower()
                    existing_mb = size * (
                        1024 if unit == "g" else 1
                    )

                    if existing_mb < 4096:
                        updated = re.sub(
                            r"^org\.gradle\.jvmargs\s*=.*$",
                            heap_line,
                            content,
                            flags=re.MULTILINE,
                        )

                        if updated != content:
                            content = updated
                            changes.append(
                                f"bumped jvmargs from '{existing}'"
                            )
            else:
                content += (
                    "\n# AndroidForge: ensure enough heap "
                    "for the Gradle daemon\n"
                    f"{heap_line}\n"
                )
                changes.append("added jvmargs")

            if not re.search(
                r"^android\.useAndroidX\s*=",
                content,
                re.MULTILINE,
            ):
                content += (
                    "\n# AndroidForge: enable AndroidX\n"
                )
                for line in androidx_lines:
                    content += f"{line}\n"

                changes.append(
                    "added useAndroidX=true + enableJetifier=true"
                )

            if changes:
                props_path.write_text(
                    content,
                    encoding="utf-8",
                )
                applied.append(
                    f"Updated {location_label}: {', '.join(changes)}"
                )

        else:
            props_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            content = (
                "# AndroidForge: ensure enough heap "
                "for the Gradle daemon\n"
                f"{heap_line}\n"
                "org.gradle.daemon=false\n"
                "\n# AndroidForge: enable AndroidX\n"
            )

            for line in androidx_lines:
                content += f"{line}\n"

            props_path.write_text(
                content,
                encoding="utf-8",
            )
            applied.append(
                f"Created {location_label} with jvmargs + "
                "useAndroidX + enableJetifier"
            )

    # 1. Root gradle.properties
    inject_gradle_properties(
        root / "gradle.properties",
        "gradle.properties (root)",
    )

    # 2. Flutter Android gradle.properties
    android_dir = root / "android"
    if android_dir.is_dir():
        inject_gradle_properties(
            android_dir / "gradle.properties",
            "android/gradle.properties (Flutter)",
        )

    # 3. Global Gradle properties
    global_properties = (
        Path.home() / ".gradle" / "gradle.properties"
    )
    inject_gradle_properties(
        global_properties,
        "~/.gradle/gradle.properties (global)",
    )

    # Inject repositories into project Gradle files.
    _inject_repositories(
        root,
        applied,
        is_flutter=bool(
            (detect.get("flutter") or {}).get("is_flutter")
        ),
    )

    return applied


def _inject_repositories(
    root: Path,
    applied: list[str],
    is_flutter: bool,
) -> None:
    """Inject common Maven repositories without corrupting file offsets."""
    candidates: list[Path] = []

    if is_flutter:
        candidates = [
            root / "android" / "build.gradle",
            root / "android" / "build.gradle.kts",
            root / "android" / "settings.gradle",
            root / "android" / "settings.gradle.kts",
        ]

    candidates += [
        root / "build.gradle",
        root / "build.gradle.kts",
        root / "settings.gradle",
        root / "settings.gradle.kts",
    ]

    repo_lines_kts = [
        'maven { url = uri("https://maven.google.com") }',
        "mavenCentral()",
        'maven { url = uri("https://plugins.gradle.org/m2") }',
        'maven { url = uri("https://jitpack.io") }',
    ]

    repo_lines_groovy = [
        "maven { url 'https://maven.google.com' }",
        "mavenCentral()",
        "maven { url 'https://plugins.gradle.org/m2' }",
        "maven { url 'https://jitpack.io' }",
    ]

    markers = [
        "maven.google.com",
        "mavenCentral()",
        "plugins.gradle.org",
        "jitpack.io",
    ]

    for grad in candidates:
        if not grad.exists():
            continue

        content = grad.read_text(
            encoding="utf-8",
            errors="replace",
        )

        already_has = (
            "jitpack.io" in content
            and "maven.google.com" in content
            and "mavenCentral()" in content
        )

        if already_has:
            continue

        is_kts = grad.name.endswith(".kts")
        repo_lines = (
            repo_lines_kts if is_kts else repo_lines_groovy
        )

        # Collect all insertions using offsets from the ORIGINAL content.
        insertions: list[tuple[int, str]] = []

        for match in re.finditer(
            r"repositories\s*\{",
            content,
            re.IGNORECASE,
        ):
            start = match.end()
            depth = 1
            index = start

            while index < len(content) and depth > 0:
                char = content[index]

                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1

                index += 1

            if depth != 0:
                continue

            close_idx = index - 1
            block_content = content[start:close_idx]

            missing = [
                line
                for line, marker in zip(repo_lines, markers)
                if marker not in block_content
            ]

            if not missing:
                continue

            inject_text = (
                "\n        // AndroidForge: ensure common public Maven repos\n"
                "        "
                + "\n        ".join(missing)
                + "\n"
            )

            insertions.append((close_idx, inject_text))

        if insertions:
            # CRITICAL FIX:
            # Apply from the highest offset to the lowest offset.
            # This prevents earlier insertions from shifting later offsets.
            new_content = content

            for close_idx, inject_text in sorted(
                insertions,
                key=lambda item: item[0],
                reverse=True,
            ):
                new_content = (
                    new_content[:close_idx]
                    + inject_text
                    + new_content[close_idx:]
                )

            injected = True

        else:
            # Fallback when no repositories block was found.
            # Note: projects using FAIL_ON_PROJECT_REPOS may reject
            # repositories declared in allprojects.
            block = (
                "\n\n"
                "// AndroidForge: ensure common public Maven repos\n"
                "allprojects {\n"
                "    repositories {\n"
            )

            for line in repo_lines:
                block += f"        {line}\n"

            block += "    }\n}\n"
            new_content = content + block
            injected = True

        if injected and new_content != content:
            grad.write_text(
                new_content,
                encoding="utf-8",
            )

            try:
                display_path = grad.relative_to(root)
            except ValueError:
                display_path = grad

            applied.append(
                "Injected JitPack/Google/mavenCentral into "
                f"{display_path}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AndroidForge toolchain setup"
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Project root path",
    )
    parser.add_argument(
        "--detect",
        required=True,
        help="Detection JSON (string or file path)",
    )
    parser.add_argument(
        "--rules",
        default=None,
        help="Path to toolchain-rules.yaml",
    )
    parser.add_argument(
        "--android-sdk",
        default=os.environ.get(
            "ANDROID_SDK_ROOT",
            "/usr/local/lib/android/sdk",
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON summary to this file",
    )

    args = parser.parse_args()
    rules = load_rules(
        Path(args.rules) if args.rules else None
    )

    if Path(args.detect).exists():
        detect = json.loads(
            Path(args.detect).read_text(encoding="utf-8")
        )
    else:
        detect = json.loads(args.detect)

    versions = detect.get("versions", {}) or {}
    wrapper = detect.get("wrapper", {}) or {}

    agp = versions.get("agp_version")
    gradle_wrapper_version = wrapper.get("version")
    java_in_build = versions.get("java_version")
    kotlin_version = versions.get("kotlin_version")

    jdk_candidates = [
        pick_jdk_for_agp(agp, rules),
        pick_jdk_for_gradle(gradle_wrapper_version, rules),
    ]

    if java_in_build:
        jdk_candidates.append(str(java_in_build))

    chosen_jdk = max(
        jdk_candidates,
        key=lambda version: (
            major(version),
            minor(version),
        ),
    )

    chosen_gradle = gradle_wrapper_version

    if not chosen_gradle:
        chosen_gradle = pick_gradle_for_agp(agp, rules)

    if not chosen_gradle:
        chosen_gradle = "8.0"

    use_wrapper = bool(wrapper.get("present"))

    if wrapper.get("uses_http"):
        use_wrapper = True

    needs_flutter = bool(
        (detect.get("flutter") or {}).get("is_flutter")
    )

    chosen_flutter = (
        pick_flutter_version(detect, rules)
        if needs_flutter
        else None
    )

    needs_ndk = bool(
        (detect.get("native") or {}).get("has_native")
    )

    chosen_ndk = (
        pick_ndk_version(detect, rules)
        if needs_ndk
        else None
    )

    fixes = determine_legacy_fixes(detect, rules)

    applied_fixes = apply_fixes(
        detect,
        fixes,
        args.android_sdk,
    )

    prep_commands: list[list[str]] = []

    if needs_flutter:
        prep_commands = [["flutter", "pub", "get"]]

        build_commands = [
            ["flutter", "build", "apk", "--debug"],
            ["flutter", "build", "apk", "--release"],
        ]

    else:
        gradlew = Path(detect["project_root"]) / "gradlew"
        gradle_cmd = (
            str(gradlew)
            if use_wrapper and gradlew.exists()
            else "gradle"
        )

        build_commands = [
            [gradle_cmd, "assembleDebug"],
            [gradle_cmd, "assembleRelease"],
            [gradle_cmd, "bundleDebug"],
        ]

    result = {
        "project_root": detect["project_root"],
        "jdk_version": chosen_jdk,
        "gradle_version": chosen_gradle,
        "agp_version": agp,
        "kotlin_version": kotlin_version,
        "ndk_version": chosen_ndk,
        "flutter_version": chosen_flutter,
        "use_gradle_wrapper": use_wrapper,
        "needs_flutter": needs_flutter,
        "needs_ndk": needs_ndk,
        "needs_gradle": detect.get("project_type")
        in ("gradle", "flutter"),
        "legacy_fixes_applied": applied_fixes,
        "prep_commands": prep_commands,
        "build_commands": build_commands,
    }

    output = json.dumps(result, indent=2)

    if args.output:
        Path(args.output).write_text(
            output,
            encoding="utf-8",
        )
    else:
        print(output)

    github_output = os.environ.get("GITHUB_OUTPUT")

    if github_output:
        with open(
            github_output,
            "a",
            encoding="utf-8",
        ) as file:
            file.write(f"jdk_version={chosen_jdk}\n")
            file.write(f"gradle_version={chosen_gradle}\n")

            if agp:
                file.write(f"agp_version={agp}\n")

            if kotlin_version:
                file.write(f"kotlin_version={kotlin_version}\n")

            if chosen_ndk:
                file.write(f"ndk_version={chosen_ndk}\n")

            file.write(
                f"needs_flutter={'true' if needs_flutter else 'false'}\n"
            )
            file.write(
                f"needs_ndk={'true' if needs_ndk else 'false'}\n"
            )
            file.write(
                "needs_gradle="
                f"{'true' if result['needs_gradle'] else 'false'}\n"
            )
            file.write(
                f"use_wrapper={'true' if use_wrapper else 'false'}\n"
            )

            if chosen_flutter:
                file.write(f"flutter_version={chosen_flutter}\n")

            file.write(
                "json<<EOF\n"
                f"{json.dumps(result)}\n"
                "EOF\n"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
