#!/usr/bin/env python3
"""Deploy and launch one immutable exact-observation sequence on a pinned host.

The launcher performs exactly one SSH transaction to the manifest-pinned
numeric host.  It creates exclusive local and remote attempt markers, installs one
content-addressed deployment without replacing mismatched files, launches the
production-write-disabled controller, inspects only its first four seconds,
and disconnects. A separately requested single-use resume keeps the original
attempt marker, proves the original pins and process quiescence, adds immutable
resume markers, and invokes the same controller with ``--resume``. Neither path
retries its SSH transaction or supervises a healthy run.

The deployment manifest and every embedded file are SHA-256 pinned.  Private
exact URL inputs are transferred in the same encrypted transaction with mode
0600 and are never copied into public receipts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FILE_MODES = {False: "0644", True: "0600"}


class LaunchError(RuntimeError):
    """The single-use deployment or launch contract was not satisfied."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_no_duplicates(text: str) -> object:
    def pairs(values: List[Tuple[str, object]]) -> Dict[str, object]:
        result = {}  # type: Dict[str, object]
        for key, value in values:
            if key in result:
                raise LaunchError("duplicate JSON key: %s" % key)
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise LaunchError("invalid deployment manifest JSON") from exc


def write_json_exclusive(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _absolute_remote(value: object, field: str) -> str:
    raw = str(value or "")
    path = Path(raw)
    if (
        not path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != raw
    ):
        raise LaunchError("%s must be a canonical absolute remote path" % field)
    return str(path)


def _deployment_relative_remote(value: object, root: str, field: str) -> str:
    """Resolve one canonical plan-relative path under the remote deployment."""

    raw = str(value or "")
    relative = Path(raw)
    if (
        not raw
        or relative.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != raw
    ):
        raise LaunchError("%s must be deployment-root-relative" % field)
    resolved = str(Path(root) / relative)
    if not _is_within(resolved, root):
        raise LaunchError("%s escapes the deployment root" % field)
    return resolved


def _numeric_ipv4(value: object) -> str:
    raw = str(value or "")
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise LaunchError("host must be a canonical numeric IPv4 address") from exc
    if parsed.version != 4 or str(parsed) != raw:
        raise LaunchError("host must be a canonical numeric IPv4 address")
    return raw


def _local_file(value: object, workspace_root: Path, field: str) -> Path:
    path = Path(str(value or ""))
    if not str(value or "") or path.is_absolute():
        raise LaunchError("%s must be workspace-root-relative" % field)
    resolved = (workspace_root / path).resolve()
    root = workspace_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise LaunchError("%s escapes the workspace root" % field)
    if not resolved.is_file():
        raise LaunchError("%s is missing" % field)
    return resolved


def _is_within(path: str, root: str) -> bool:
    candidate = Path(path)
    base = Path(root)
    return candidate == base or base in candidate.parents


def deployment_digest(files: Sequence[Mapping[str, object]], deployment_root: str) -> str:
    digest = hashlib.sha256()
    rows = []
    for item in files:
        remote = str(item["remote_path"])
        if not _is_within(remote, deployment_root):
            raise LaunchError("deployment file is outside the content-addressed root")
        relative = Path(remote).relative_to(Path(deployment_root)).as_posix()
        rows.append((
            relative, str(item["sha256"]), int(item["bytes"]), str(item["mode"]),
        ))
    for relative, file_hash, size, mode in sorted(rows):
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(file_hash.encode("ascii") + b"\0")
        digest.update(str(size).encode("ascii") + b"\0")
        digest.update(mode.encode("ascii") + b"\0")
    return digest.hexdigest()


def load_manifest(path: Path, expected_sha256: str, workspace_root: Path) -> Dict[str, object]:
    if not HEX64.fullmatch(expected_sha256) or sha256_path(path) != expected_sha256:
        raise LaunchError("deployment manifest SHA-256 mismatch")
    value = json_no_duplicates(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LaunchError("deployment manifest must be a JSON object")
    if (
        value.get("schema_version") != 1
        or value.get("mode") != "exact_sequence_single_use_deployment"
        or value.get("production_writes") is not False
        or value.get("ssh_connections") != 1
        or value.get("ssh_retries") != 0
        or value.get("startup_inspection_seconds") != 4
        or value.get("network_ceiling") != 2
        or value.get("browser_ceiling") != 0
    ):
        raise LaunchError("unsafe single-use deployment header")
    host = _numeric_ipv4(value.get("host"))
    sequence_id = str(value.get("sequence_id", ""))
    if not SAFE_ID.fullmatch(sequence_id):
        raise LaunchError("invalid sequence_id")
    if (
        value.get("repository") != "lin61302/scraping"
        or value.get("branch") != "main"
        or not HEX40.fullmatch(str(value.get("code_git_commit", "")))
    ):
        raise LaunchError("invalid GitHub provenance gate")
    deployment_root = _absolute_remote(value.get("deployment_root_remote"), "deployment_root_remote")
    if not HEX64.fullmatch(str(value.get("deployment_sha256", ""))):
        raise LaunchError("invalid deployment SHA-256")
    raw_files = value.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise LaunchError("files must be a nonempty deployment list")
    files = []  # type: List[Dict[str, object]]
    remote_paths = set()
    for index, raw in enumerate(raw_files):
        if not isinstance(raw, Mapping):
            raise LaunchError("deployment file must be an object")
        local = _local_file(raw.get("local_path"), workspace_root, "files[%s]" % index)
        remote = _absolute_remote(raw.get("remote_path"), "files[%s].remote_path" % index)
        digest = str(raw.get("sha256", ""))
        if not HEX64.fullmatch(digest) or sha256_path(local) != digest:
            raise LaunchError("deployment file SHA-256 mismatch")
        if remote in remote_paths or not _is_within(remote, deployment_root):
            raise LaunchError("duplicate or out-of-root deployment path")
        if not isinstance(raw.get("private"), bool):
            raise LaunchError("deployment file privacy class must be explicit")
        private = bool(raw["private"])
        mode = str(raw.get("mode", ""))
        if mode != FILE_MODES[private]:
            raise LaunchError("deployment file mode does not match its privacy class")
        remote_paths.add(remote)
        files.append({
            "local_path": str(local),
            "remote_path": remote,
            "sha256": digest,
            "bytes": local.stat().st_size,
            "private": private,
            "mode": mode,
        })
    if deployment_digest(files, deployment_root) != value["deployment_sha256"]:
        raise LaunchError("content-addressed deployment digest mismatch")
    by_remote = {str(item["remote_path"]): item for item in files}
    for field in ("controller_remote", "plan_remote"):
        remote = _absolute_remote(value.get(field), field)
        if remote not in by_remote:
            raise LaunchError("%s is not present in the deployment" % field)
    plan_sha = str(value.get("plan_sha256", ""))
    if not HEX64.fullmatch(plan_sha) or by_remote[str(value["plan_remote"])]["sha256"] != plan_sha:
        raise LaunchError("plan pin does not match its deployment entry")
    if (
        by_remote[str(value["plan_remote"])]["private"] is not True
        or by_remote[str(value["plan_remote"])]["mode"] != "0600"
    ):
        raise LaunchError("the deployment plan must remain private with mode 0600")
    controller_sha = str(value.get("controller_sha256", ""))
    if (
        not HEX64.fullmatch(controller_sha)
        or by_remote[str(value["controller_remote"])]["sha256"] != controller_sha
    ):
        raise LaunchError("controller pin does not match its deployment entry")
    if (
        by_remote[str(value["controller_remote"])]["private"] is not False
        or by_remote[str(value["controller_remote"])]["mode"] != "0644"
    ):
        raise LaunchError("the public controller must use mode 0644")
    plan_local = Path(str(by_remote[str(value["plan_remote"])]["local_path"]))
    plan_value = json_no_duplicates(plan_local.read_text(encoding="utf-8"))
    if (
        not isinstance(plan_value, Mapping)
        or plan_value.get("mode") != "unattended_exact_observation_sequence"
        or plan_value.get("production_writes") is not False
    ):
        raise LaunchError("deployed plan is not production-write-disabled")
    phases = plan_value.get("phases")
    if not isinstance(phases, list) or not phases:
        raise LaunchError("deployed plan phases are missing")
    for phase_index, phase in enumerate(phases):
        if not isinstance(phase, Mapping) or not isinstance(phase.get("lanes"), list):
            raise LaunchError("deployed plan phase is invalid")
        for lane_index, lane in enumerate(phase["lanes"]):
            if not isinstance(lane, Mapping) or lane.get("kind") != "exact_urls":
                continue
            exact_input = lane.get("exact_input")
            if not isinstance(exact_input, Mapping):
                raise LaunchError("exact lane input pin is missing")
            exact_remote = _deployment_relative_remote(
                exact_input.get("path"),
                deployment_root,
                "phases[%s].lanes[%s].exact_input.path" % (phase_index, lane_index),
            )
            deployed = by_remote.get(exact_remote)
            if deployed is None or deployed["private"] is not True or deployed["mode"] != "0600":
                raise LaunchError("every exact URL input must be a private 0600 deployment file")
            exact_sha = str(exact_input.get("sha256", ""))
            if not HEX64.fullmatch(exact_sha) or deployed["sha256"] != exact_sha:
                raise LaunchError("exact URL input plan pin differs from its deployment pin")
    automation_root = _absolute_remote(value.get("automation_root_remote"), "automation_root_remote")
    if not _is_within(automation_root, deployment_root):
        raise LaunchError("automation root is outside the deployment")
    for field in (
        "python_remote", "output_root_remote", "receipt_root_remote",
        "host_lock_remote", "remote_attempt_marker", "deployment_receipt_remote",
        "remote_resume_marker", "resume_launch_receipt_remote",
        "controller_log_remote", "resume_controller_log_remote",
        "startup_receipt_remote", "terminal_receipt_remote",
    ):
        _absolute_remote(value.get(field), field)
    exclusive_paths = [str(value[field]) for field in (
        "remote_attempt_marker", "remote_resume_marker", "resume_launch_receipt_remote",
        "deployment_receipt_remote", "controller_log_remote", "resume_controller_log_remote",
        "startup_receipt_remote", "terminal_receipt_remote",
    )]
    if len(exclusive_paths) != len(set(exclusive_paths)):
        raise LaunchError("remote marker, receipt, and log paths must be unique")
    parents = value.get("allowed_parent_roots_remote")
    if not isinstance(parents, list) or not parents:
        raise LaunchError("allowed_parent_roots_remote must be nonempty")
    for index, parent in enumerate(parents):
        _absolute_remote(parent, "allowed_parent_roots_remote[%s]" % index)
    result = dict(value)
    result["host"] = host
    result["files"] = files
    result["manifest_sha256"] = expected_sha256
    return result


def build_payload(
    manifest: Mapping[str, object], *, resume_existing_attempt: bool = False,
) -> Dict[str, object]:
    embedded = []
    for item in manifest["files"]:
        assert isinstance(item, Mapping)
        payload = Path(str(item["local_path"])).read_bytes()
        embedded.append({
            "remote_path": item["remote_path"],
            "sha256": item["sha256"],
            "bytes": item["bytes"],
            "private": item["private"],
            "mode": item["mode"],
            "base64": base64.b64encode(payload).decode("ascii"),
        })
    return {
        "schema_version": 1,
        "sequence_id": manifest["sequence_id"],
        "production_writes": False,
        "host": manifest["host"],
        "repository": manifest["repository"],
        "branch": manifest["branch"],
        "code_git_commit": manifest["code_git_commit"],
        "manifest_sha256": manifest["manifest_sha256"],
        "deployment_sha256": manifest["deployment_sha256"],
        "deployment_root": manifest["deployment_root_remote"],
        "files": embedded,
        "python": manifest["python_remote"],
        "controller": manifest["controller_remote"],
        "controller_sha256": manifest["controller_sha256"],
        "plan": manifest["plan_remote"],
        "plan_sha256": manifest["plan_sha256"],
        "automation_root": manifest["automation_root_remote"],
        "output_root": manifest["output_root_remote"],
        "receipt_root": manifest["receipt_root_remote"],
        "host_lock": manifest["host_lock_remote"],
        "allowed_parent_roots": manifest["allowed_parent_roots_remote"],
        "remote_attempt_marker": manifest["remote_attempt_marker"],
        "remote_resume_marker": manifest["remote_resume_marker"],
        "resume_launch_receipt": manifest["resume_launch_receipt_remote"],
        "deployment_receipt": manifest["deployment_receipt_remote"],
        "controller_log": manifest["controller_log_remote"],
        "resume_controller_log": manifest["resume_controller_log_remote"],
        "startup_receipt": manifest["startup_receipt_remote"],
        "terminal_receipt": manifest["terminal_receipt_remote"],
        "startup_inspection_seconds": 4,
        "network_ceiling": 2,
        "browser_ceiling": 0,
        "resume_existing_attempt": resume_existing_attempt,
    }


def remote_program(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"))
    return r'''from __future__ import print_function
import base64, hashlib, ipaddress, json, os, shutil, signal, stat, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

P=json.loads(%r)
def now(): return datetime.now(timezone.utc).isoformat()
def sha(path):
 d=hashlib.sha256()
 with Path(path).open("rb") as stream:
  for chunk in iter(lambda:stream.read(1024*1024),b""): d.update(chunk)
 return d.hexdigest()
def create_json(path,value):
 target=Path(path); target.parent.mkdir(parents=True,exist_ok=True)
 fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,"w") as stream:
  json.dump(value,stream,indent=2,sort_keys=True); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
def decode(item):
 data=base64.b64decode(item["base64"],validate=True)
 if len(data)!=item["bytes"] or hashlib.sha256(data).hexdigest()!=item["sha256"]:
  raise RuntimeError("embedded deployment file mismatch")
 return data
def install(item):
 path=Path(item["remote_path"]); root=Path(P["deployment_root"]).resolve(); resolved=path.resolve()
 if not (resolved==root or root in resolved.parents): raise RuntimeError("deployment path escaped root")
 expected_mode=item.get("mode")
 if item.get("private") is not True and item.get("private") is not False: raise RuntimeError("missing deployment privacy class")
 if item.get("private") is True and expected_mode!="0600": raise RuntimeError("unsafe private deployment mode")
 if item.get("private") is False and expected_mode!="0644": raise RuntimeError("unsafe public deployment mode")
 if expected_mode not in ("0600","0644"): raise RuntimeError("unsupported deployment mode")
 mode=int(expected_mode,8); data=decode(item); path.parent.mkdir(parents=True,exist_ok=True)
 if path.exists():
  if path.is_symlink() or not path.is_file() or sha(path)!=item["sha256"]: raise RuntimeError("existing deployment file mismatch")
  os.chmod(str(path),mode)
  if stat.S_IMODE(path.stat().st_mode)!=mode: raise RuntimeError("existing deployment mode mismatch")
  return {"sha256":item["sha256"],"bytes":item["bytes"],"mode":expected_mode,"status":"reused_matching"}
 flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)
 fd=os.open(str(path),flags,mode)
 with os.fdopen(fd,"wb") as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
 os.chmod(str(path),mode)
 if sha(path)!=item["sha256"] or path.stat().st_size!=item["bytes"] or stat.S_IMODE(path.stat().st_mode)!=mode:
  raise RuntimeError("created deployment file verification failed")
 return {"sha256":sha(path),"bytes":path.stat().st_size,"mode":expected_mode,"status":"created"}
def read_json(path): return json.loads(Path(path).read_text(encoding="utf-8"))
def process_counts():
 text=subprocess.run(["ps","-eo","pid=,args="],capture_output=True,text=True,check=True).stdout
 controller=[]; observers=[]; browsers=[]
 for line in text.splitlines():
  pieces=line.strip().split(None,1)
  if len(pieces)!=2: continue
  pid=int(pieces[0]); command=pieces[1]; lower=command.lower()
  if P["controller"] in command and P["plan"] in command: controller.append(pid)
  belongs=P["deployment_root"] in command or P["output_root"] in command
  if belongs and ("parallel_observe_pure.py" in command or "run_pure_discovery_diagnostic.py" in command): observers.append(pid)
  if belongs and any(value in lower for value in ("chromedriver","geckodriver","playwright","--headless","selenium")): browsers.append(pid)
 return {"matching_controllers":controller,"matching_observers":observers,"matching_browsers":browsers,
         "observer_process_count":len(observers),"browser_process_count":len(browsers)}
def memory_available_gib():
 values={}
 for line in Path("/proc/meminfo").read_text().splitlines():
  key,raw=line.split(":",1); values[key]=int(raw.strip().split()[0])
 return round(values.get("MemAvailable",0)/1024/1024,3)
def snapshot():
 disk=shutil.disk_usage("/"); processes=process_counts()
 return {"captured_at_utc":now(),"load_averages":list(os.getloadavg()),"cpu_count":os.cpu_count(),
         "memory_available_gib":memory_available_gib(),"root_free_gib":round(disk.free/1024**3,3),
         "processes":processes}
def group_alive(pgid):
 try: os.killpg(pgid,0)
 except ProcessLookupError: return False
 except PermissionError: return True
 return True
def controller_quiescent(process):
 process.poll()
 counts=process_counts()
 quiet=(not group_alive(process.pid) and not counts["matching_controllers"]
        and not counts["matching_observers"] and not counts["matching_browsers"])
 return quiet,counts
def signal_groups(pids,sig):
 sent=[]
 for pid in sorted(set(pids)):
  try:
   pgid=os.getpgid(pid)
   if pgid not in sent: os.killpg(pgid,sig); sent.append(pgid)
  except ProcessLookupError: pass
 return sent
def stop(process):
 pid=process.pid; sent_term=[]; sent_kill=[]
 quiescent,counts=controller_quiescent(process)
 if quiescent: return {"verified":True,"sigterm_groups":[],"sigkill_groups":[],"processes":counts}
 targets=[pid]+counts["matching_controllers"]+counts["matching_observers"]+counts["matching_browsers"]
 sent_term=signal_groups(targets,signal.SIGTERM)
 deadline=time.monotonic()+10
 while time.monotonic()<deadline:
  quiescent,counts=controller_quiescent(process)
  if quiescent: return {"verified":True,"sigterm_groups":sent_term,"sigkill_groups":[],"processes":counts}
  time.sleep(.2)
 targets=[pid]+counts["matching_controllers"]+counts["matching_observers"]+counts["matching_browsers"]
 sent_kill=signal_groups(targets,signal.SIGKILL)
 deadline=time.monotonic()+5
 while time.monotonic()<deadline:
  quiescent,counts=controller_quiescent(process)
  if quiescent: return {"verified":True,"sigterm_groups":sent_term,"sigkill_groups":sent_kill,"processes":counts}
  time.sleep(.1)
 quiescent,counts=controller_quiescent(process)
 if not quiescent: raise RuntimeError("controller process group remained alive after SIGKILL")
 return {"verified":True,"sigterm_groups":sent_term,"sigkill_groups":sent_kill,"processes":counts}
def launch_controller(command,resume=False):
 log=Path(P["resume_controller_log"] if resume else P["controller_log"]); startup=Path(P["startup_receipt"]); terminal=Path(P["terminal_receipt"])
 log.parent.mkdir(parents=True,exist_ok=True); process=None
 try:
  with log.open("xb") as stream:
   process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
  os.chmod(str(log),0o600)
  if resume:
   create_json(P["resume_launch_receipt"],{"schema_version":1,"kind":"exact_sequence_resume_launch",
               "sequence_id":P["sequence_id"],"started_at_utc":now(),"pid":process.pid,
               "manifest_sha256":P["manifest_sha256"],"deployment_sha256":P["deployment_sha256"],
               "plan_sha256":P["plan_sha256"],"production_writes":False})
  time.sleep(P["startup_inspection_seconds"]); exit_after=process.poll(); cleanup=None
  if exit_after is not None: cleanup=stop(process)
  if not startup.is_file():
   if cleanup is None: cleanup=stop(process)
   raise RuntimeError("controller startup receipt missing after bounded inspection")
  receipt=read_json(startup)
  if (receipt.get("production_writes") is not False or receipt.get("plan_sha256")!=P["plan_sha256"]
      or receipt.get("sequence_id")!=P["sequence_id"]):
   if cleanup is None: cleanup=stop(process)
   raise RuntimeError("unsafe startup receipt")
  if not resume and receipt.get("pid")!=process.pid:
   if cleanup is None: cleanup=stop(process)
   raise RuntimeError("startup PID mismatch")
  if exit_after not in (None,0): raise RuntimeError("controller exited during startup after verified cleanup")
  if exit_after==0 and not terminal.is_file(): raise RuntimeError("controller exited without terminal receipt after verified cleanup")
  terminal_sha256=None
  if exit_after==0:
   terminal_value=read_json(terminal)
   if (terminal_value.get("production_writes") is not False or terminal_value.get("plan_sha256")!=P["plan_sha256"]
       or terminal_value.get("sequence_id")!=P["sequence_id"]):
    raise RuntimeError("unsafe terminal receipt after verified cleanup")
   terminal_sha256=sha(terminal)
  return {"status":"launched_unattended" if exit_after is None else "completed_fast",
          "controller_pid":process.pid,"exit_after_four_seconds":exit_after,
          "startup_receipt_sha256":sha(startup),"deployment_receipt_sha256":sha(P["deployment_receipt"]),
          "terminal_receipt_sha256":terminal_sha256,
          "resume_existing_attempt":resume,
          "resume_launch_receipt_sha256":sha(P["resume_launch_receipt"]) if resume else None,
          "controller_log_sha256":sha(log),"controller_log_bytes":log.stat().st_size,
          "early_exit_cleanup":cleanup}
 except Exception:
  if process is not None: stop(process)
  raise
def main():
 os.umask(0o077)
 try: host=ipaddress.ip_address(P.get("host",""))
 except ValueError: raise RuntimeError("unsafe launch host")
 if host.version!=4 or str(host)!=P.get("host") or P.get("production_writes") is not False: raise RuntimeError("unsafe launch payload")
 if P.get("network_ceiling")!=2 or P.get("browser_ceiling")!=0 or P.get("startup_inspection_seconds")!=4:
  raise RuntimeError("unsafe launch ceiling or startup bound")
 before=snapshot(); marker=Path(P["remote_attempt_marker"]); resume_marker=Path(P["remote_resume_marker"]); terminal=Path(P["terminal_receipt"])
 resume=bool(P.get("resume_existing_attempt"))
 result={"schema_version":1,"sequence_id":P["sequence_id"],"production_writes":False,
         "checked_at_utc":now(),"manifest_sha256":P["manifest_sha256"],
         "deployment_sha256":P["deployment_sha256"],"capacity_and_process_snapshot":before}
 if terminal.exists():
  receipt=read_json(terminal)
  if (receipt.get("production_writes") is not False or receipt.get("plan_sha256")!=P["plan_sha256"]
      or receipt.get("sequence_id")!=P["sequence_id"]): raise RuntimeError("unsafe terminal receipt")
  result["status"]="already_terminal_no_launch"; result["terminal_sha256"]=sha(terminal); print(json.dumps(result,sort_keys=True)); return 0
 if marker.exists() and not resume:
  result["status"]="remote_attempt_marker_exists_no_launch"; print(json.dumps(result,sort_keys=True)); return 0
 if resume and not marker.exists(): raise RuntimeError("resume requires the immutable original attempt marker")
 if (before["processes"]["matching_controllers"] or before["processes"]["matching_observers"]
     or before["processes"]["matching_browsers"]):
  result["status"]="matching_sequence_process_without_marker_no_launch"; print(json.dumps(result,sort_keys=True)); return 1
 if resume:
  original=read_json(marker)
  if (original.get("kind")!="exact_sequence_remote_attempt" or original.get("sequence_id")!=P["sequence_id"]
      or original.get("manifest_sha256")!=P["manifest_sha256"]
      or original.get("deployment_sha256")!=P["deployment_sha256"]
      or original.get("plan_sha256")!=P["plan_sha256"]
      or original.get("production_writes") is not False): raise RuntimeError("original attempt marker pins are invalid")
  if resume_marker.exists(): raise RuntimeError("single-use resume marker already exists")
  if not Path(P["deployment_receipt"]).is_file() or not Path(P["startup_receipt"]).is_file():
   raise RuntimeError("resume requires pinned deployment and startup receipts")
  deployment=read_json(P["deployment_receipt"]); startup_value=read_json(P["startup_receipt"])
  if (deployment.get("sequence_id")!=P["sequence_id"] or deployment.get("manifest_sha256")!=P["manifest_sha256"]
      or deployment.get("deployment_sha256")!=P["deployment_sha256"] or deployment.get("production_writes") is not False
      or deployment.get("plan_sha256")!=P["plan_sha256"]
      or startup_value.get("sequence_id")!=P["sequence_id"] or startup_value.get("plan_sha256")!=P["plan_sha256"]
      or startup_value.get("production_writes") is not False): raise RuntimeError("resume receipt pins are invalid")
  create_json(resume_marker,{"schema_version":1,"kind":"exact_sequence_remote_resume_attempt",
              "sequence_id":P["sequence_id"],"started_at_utc":now(),"manifest_sha256":P["manifest_sha256"],
              "deployment_sha256":P["deployment_sha256"],"plan_sha256":P["plan_sha256"],
              "original_attempt_sha256":sha(marker),"startup_receipt_sha256":sha(P["startup_receipt"]),
              "production_writes":False})
 else:
  create_json(marker,{"schema_version":1,"kind":"exact_sequence_remote_attempt","sequence_id":P["sequence_id"],
                      "started_at_utc":now(),"manifest_sha256":P["manifest_sha256"],
                      "deployment_sha256":P["deployment_sha256"],"plan_sha256":P["plan_sha256"],
                      "production_writes":False})
 installed=[install(item) for item in P["files"]]
 if sha(P["controller"])!=P["controller_sha256"] or sha(P["plan"])!=P["plan_sha256"]: raise RuntimeError("post-install pin mismatch")
 version=subprocess.run([P["python"],"-c","import sys; print('%%d.%%d'%%sys.version_info[:2])"],capture_output=True,text=True,timeout=10,check=False)
 if version.returncode!=0 or tuple(int(v) for v in version.stdout.strip().split("."))<(3,8): raise RuntimeError("Python 3.8 or newer is required")
 deployment={"schema_version":1,"kind":"exact_sequence_deployment","sequence_id":P["sequence_id"],
             "created_at_utc":now(),"manifest_sha256":P["manifest_sha256"],
             "deployment_sha256":P["deployment_sha256"],"plan_sha256":P["plan_sha256"],
             "file_count":len(installed),
             "files":installed,"python_version":version.stdout.strip(),"capacity_before_launch":before,
             "production_writes":False}
 if not resume: create_json(P["deployment_receipt"],deployment)
 command=[P["python"],P["controller"],"--plan",P["plan"],"--plan-sha256",P["plan_sha256"],
          "--deployment-root",P["deployment_root"],"--automation-root",P["automation_root"],
          "--output-root",P["output_root"],"--receipt-root",P["receipt_root"],
          "--host-lock-path",P["host_lock"],"--python",P["python"]]
 for root in P["allowed_parent_roots"]: command.extend(["--allowed-parent-root",root])
 if resume: command.append("--resume")
 result.update(launch_controller(command,resume=resume))
 print(json.dumps(result,sort_keys=True)); return 0
try: raise SystemExit(main())
except Exception as exc:
 print(json.dumps({"status":"failed","sequence_id":P.get("sequence_id"),"error":type(exc).__name__},sort_keys=True)); raise
''' % encoded


def preflight(
    manifest_path: Path, manifest_sha256: str, workspace_root: Path,
) -> Mapping[str, object]:
    manifest = load_manifest(manifest_path, manifest_sha256, workspace_root)
    payload = build_payload(manifest)
    program = remote_program(payload)
    compile(program, "<exact-sequence-launch>", "exec")
    return {
        "status": "pass",
        "sequence_id": manifest["sequence_id"],
        "manifest_sha256": manifest_sha256,
        "deployment_sha256": manifest["deployment_sha256"],
        "plan_sha256": manifest["plan_sha256"],
        "controller_sha256": manifest["controller_sha256"],
        "embedded_file_count": len(manifest["files"]),
        "embedded_payload_bytes": sum(int(item["bytes"]) for item in manifest["files"]),
        "remote_program_sha256": sha256_bytes(program.encode("utf-8")),
        "ssh_connections": 1,
        "ssh_retries": 0,
        "production_writes": False,
    }


def execute(args: argparse.Namespace) -> int:
    if args.ssh_timeout_seconds < 30:
        raise LaunchError("SSH timeout must cover the bounded startup inspection")
    operation_root = args.operation_root.resolve()
    marker = operation_root / "attempt_receipt.json"
    resume = bool(getattr(args, "resume_existing_attempt", False))
    resume_marker = operation_root / "resume_attempt_receipt.json"
    if resume:
        if not marker.is_file():
            raise LaunchError("resume requires the immutable local attempt marker")
        if resume_marker.exists():
            raise LaunchError("local single-use resume marker already exists")
    elif marker.exists():
        raise LaunchError("local attempt marker already exists")
    manifest = load_manifest(
        args.manifest.resolve(), args.manifest_sha256.lower(), args.workspace_root.resolve(),
    )
    payload = build_payload(manifest, resume_existing_attempt=resume)
    program = remote_program(payload)
    attempt_payload = {
        "schema_version": 1,
        "kind": "exact_sequence_local_attempt",
        "sequence_id": manifest["sequence_id"],
        "started_at_utc": iso_now(),
        "host_class": "manifest_pinned_numeric_ipv4",
        "manifest_sha256": args.manifest_sha256.lower(),
        "deployment_sha256": manifest["deployment_sha256"],
        "plan_sha256": manifest["plan_sha256"],
        "remote_program_sha256": sha256_bytes(program.encode("utf-8")),
        "ssh_connections_allowed": 1,
        "ssh_retries_allowed": 0,
        "production_writes": False,
    }
    if resume:
        original = json_no_duplicates(marker.read_text(encoding="utf-8"))
        if not isinstance(original, Mapping) or any(
            original.get(key) != value for key, value in attempt_payload.items()
            if key not in {"started_at_utc", "remote_program_sha256"}
        ):
            raise LaunchError("local original attempt marker pins are invalid")
        write_json_exclusive(resume_marker, {
            "schema_version": 1,
            "kind": "exact_sequence_local_resume_attempt",
            "sequence_id": manifest["sequence_id"],
            "started_at_utc": iso_now(),
            "original_attempt_sha256": sha256_path(marker),
            "host_class": "manifest_pinned_numeric_ipv4",
            "manifest_sha256": args.manifest_sha256.lower(),
            "deployment_sha256": manifest["deployment_sha256"],
            "plan_sha256": manifest["plan_sha256"],
            "remote_program_sha256": sha256_bytes(program.encode("utf-8")),
            "ssh_connections_allowed": 1,
            "ssh_retries_allowed": 0,
            "production_writes": False,
        })
    else:
        write_json_exclusive(marker, attempt_payload)
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
        "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2", str(manifest["host"]), "python3", "-",
    ]
    started = iso_now()
    timed_out = False
    try:
        completed = subprocess.run(
            command, input=program, text=True, capture_output=True,
            timeout=args.ssh_timeout_seconds, check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
    except Exception as exc:
        exit_code = 125
        stdout = ""
        stderr = "%s:%s" % (type(exc).__name__, str(exc))
    prefix = "resume_" if resume else ""
    write_private(operation_root / (prefix + "ssh_stdout.private.json"), stdout.encode("utf-8"))
    write_private(operation_root / (prefix + "ssh_stderr.private.log"), stderr.encode("utf-8"))
    try:
        remote = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        remote = None
    receipt = {
        "schema_version": 1,
        "kind": (
            "exact_sequence_resume_connection_terminal" if resume
            else "exact_sequence_connection_terminal"
        ),
        "sequence_id": manifest["sequence_id"],
        "started_at_utc": started,
        "finished_at_utc": iso_now(),
        "host_class": "manifest_pinned_numeric_ipv4",
        "ssh_connections": 1,
        "retry_attempted": False,
        "resume_existing_attempt": resume,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stdout_sha256": sha256_bytes(stdout.encode("utf-8")),
        "stderr_sha256": sha256_bytes(stderr.encode("utf-8")),
        "remote_status": remote.get("status") if isinstance(remote, Mapping) else None,
        "manifest_sha256": args.manifest_sha256.lower(),
        "deployment_sha256": manifest["deployment_sha256"],
        "plan_sha256": manifest["plan_sha256"],
        "production_writes": False,
    }
    write_json_exclusive(
        operation_root / ("resume_connection_receipt.json" if resume else "connection_receipt.json"),
        receipt,
    )
    accepted = {
        "launched_unattended", "completed_fast", "already_terminal_no_launch",
    }
    return 0 if exit_code == 0 and receipt["remote_status"] in accepted else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", required=True, type=Path)
    result.add_argument("--manifest-sha256", required=True)
    result.add_argument("--workspace-root", required=True, type=Path)
    result.add_argument("--operation-root", required=True, type=Path)
    result.add_argument("--ssh-timeout-seconds", type=int, default=120)
    result.add_argument("--preflight-only", action="store_true")
    result.add_argument("--resume-existing-attempt", action="store_true")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.preflight_only:
            result = preflight(
                args.manifest.resolve(), args.manifest_sha256.lower(),
                args.workspace_root.resolve(),
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        return execute(args)
    except Exception as exc:
        print(json.dumps({"status": "refused", "error": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
