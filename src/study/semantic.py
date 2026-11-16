"""LLM-assisted semantic assessment module.

Preparation and validation are offline. New inference requires an explicit
execution flag, an exact call count, and credentials supplied by the caller.
"""
from __future__ import annotations
import copy
import csv
import hashlib
import itertools
import json
import os
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from .release import ROOT, read_csv, read_json, write_csv
from .semantic_history import _build_trial
from .semantic_candidates import parse_assessment
from .semantic_io import canonical_json

def request_body(trial_input):
    body=copy.deepcopy(read_json(ROOT/"data/semantic_assessment/request_profile.json"))
    body["instructions"]=(ROOT/"prompts/semantic_assessment.txt").read_text(encoding="utf-8")
    body["input"]=canonical_json(trial_input)
    labels=list(dict.fromkeys([f"V{s['version']}" for s in trial_input["states"]]+[
        f"V{candidate[key]}" for candidate in trial_input["reclassification_candidates"] for key in ["from_version","to_version"]]))
    body["text"]["format"]["schema"]["properties"]["versions"]["items"]["enum"]=labels
    return body

def prepare_assessments(run_dir, output_dir):
    run_dir,output_dir=Path(run_dir).resolve(),Path(output_dir).resolve()
    if output_dir.exists():
        raise FileExistsError("Refusing to overwrite prepared semantic assessments")
    output_dir.mkdir(parents=True)
    expected={r["nct_id"]:r for r in read_csv(ROOT/"data/semantic_assessment/assessments.csv")}
    by_nct=defaultdict(list)
    for row in read_csv(run_dir/"processed/version_snapshots.csv"):
        by_nct[row["nct_id"]].append(row)
    if set(by_nct)!=set(expected):
        raise ValueError("Semantic assessment cohort does not match the frozen study")
    totals=Counter()
    with (run_dir/"processed/outcomes.csv").open(encoding="utf-8-sig",newline="") as handle:
        grouped=iter(itertools.groupby(csv.DictReader(handle),key=lambda r:r["nct_id"]))
        current=next(grouped,None)
        for number,nct in enumerate(sorted(by_nct),1):
            extracted=defaultdict(list)
            if current and current[0]<nct:
                raise ValueError("Unexpected or unsorted outcome trial")
            if current and current[0]==nct:
                for row in current[1]:
                    if row["outcome_type"] in {"PRIMARY","SECONDARY"}:
                        extracted[(int(row["version"]),row["outcome_type"])].append(row)
                current=next(grouped,None)
            trial_input,_,_,meta=_build_trial(run_dir,nct,by_nct[nct],extracted)
            serialized=canonical_json(trial_input)
            digest=hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            saved=expected[nct]
            if digest!=saved["prepared_input_sha256"]:
                raise ValueError(f"Frozen semantic input changed: {nct}")
            diagnostic={"meaningful_change":saved["meaningful_change"].lower()=="true",
                        **{k:json.loads(saved[k]) for k in ["change_type","versions","outcome_reclassifications"]}}
            parse_assessment(json.dumps(diagnostic),trial_input)
            deterministic=len(trial_input["states"])==1 and not trial_input["reclassification_candidates"]
            if deterministic!=(saved["result_origin"]=="deterministic_no_comparison"):
                raise ValueError(f"Deterministic semantic eligibility changed: {nct}")
            if deterministic and diagnostic["meaningful_change"]:
                raise ValueError("Deterministic no-comparison result cannot be positive")
            (output_dir/f"{nct}.json").write_text(serialized+"\n",encoding="utf-8")
            totals.update(trials=1,raw_versions=meta["raw_versions"],retained_states=meta["retained_states"],
                          candidate_pairs=meta["candidate_pairs"],deterministic=int(deterministic))
            if number%100==0:
                print(f"Semantic assessment inputs verified: {number}/1758",flush=True)
        if current is not None:
            raise ValueError("Unmatched extracted outcomes")
    target={"trials":1758,"raw_versions":54631,"retained_states":4803,"candidate_pairs":953,"deterministic":391}
    if dict(totals)!=target:
        raise ValueError(f"Semantic preparation counts differ: {dict(totals)}")
    manifest={"module":"LLM-assisted semantic assessment module","model":"gpt-5.6-sol","reasoning_effort":"medium",
              "counts":dict(totals),"files":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output_dir.glob('NCT*.json'))}}
    (output_dir/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    print("All 1,758 semantic assessment inputs and saved responses verified.",flush=True)
    return manifest

def execute_assessments(prepared_dir, output_dir, *, execute=False, expected_calls=None):
    if not execute:
        raise ValueError("New inference requires --execute")
    prepared_dir,output_dir=Path(prepared_dir).resolve(),Path(output_dir).resolve()
    manifest=read_json(prepared_dir/"manifest.json")
    output_dir.mkdir(parents=True,exist_ok=True)
    if list(output_dir.glob('*.inflight')):
        raise RuntimeError("An earlier request has an uncertain outcome; resolve it before resuming")
    pending=[]
    for name,digest in manifest["files"].items():
        path=prepared_dir/name
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError(f"Prepared semantic input changed: {name}")
        trial_input=read_json(path)
        if (output_dir/name).exists():
            parse_assessment(json.dumps(read_json(output_dir/name)),trial_input)
            continue
        if len(trial_input["states"])==1 and not trial_input["reclassification_candidates"]:
            result={"meaningful_change":False,"change_type":[],"versions":[],"outcome_reclassifications":[]}
            (output_dir/name).write_text(canonical_json(result)+"\n",encoding="utf-8")
        else:
            pending.append((name,trial_input))
    if expected_calls!=len(pending):
        raise ValueError(f"Expected-call count must equal {len(pending)}")
    key=os.environ.get("OPENAI_API_KEY","")
    if pending and not key:
        raise RuntimeError("New inference requires caller-configured API credentials")
    for name,trial_input in pending:
        marker=output_dir/(name+".inflight")
        marker.write_text("A request may have reached the API. Do not retry automatically.\n",encoding="utf-8")
        request=urllib.request.Request("https://api.openai.com/v1/responses",
            data=canonical_json(request_body(trial_input)).encode("utf-8"),
            headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(request,timeout=600) as response:
            payload=json.load(response)
        texts=[part["text"] for item in payload.get("output",[]) if item.get("type")=="message"
               for part in item.get("content",[]) if part.get("type")=="output_text"]
        result=parse_assessment("".join(texts),trial_input)["diagnostic"]
        (output_dir/name).write_text(canonical_json(result)+"\n",encoding="utf-8")
        marker.unlink()
        print(f"Completed {trial_input['nct_id']}",flush=True)
        time.sleep(.5)
