import argparse
import csv
import json
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def require_args(args: argparse.Namespace, required_fields: list[str]) -> None:
    missing = [field for field in required_fields if not getattr(args, field, None)]
    if missing:
        raise SystemExit(f"Missing required configuration/arguments: {', '.join(missing)}")


def load_env(env_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    # Environment variables override .env values.
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith("STEP") or k.startswith("BATCH") or k.startswith("OPENAI_")})
    return env


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def read_species_profiles(csv_path: Path) -> list[dict[str, str]]:
    profiles: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            normalized = {str(k).strip().lstrip("\ufeff"): v for k, v in row.items()}
            profiles.append(
                {
                    "name": normalized["name"],
                    "scale": normalized["physical_scale"],
                    "form": normalized["morphological_form"],
                    "kinematics": normalized["kinematic_profile"],
                    "space": normalized["primary_spatial_niche"],
                    "eco_status": normalized["urban_ecological_status"],
                    "econ_effect": normalized["economic_effect"],
                    "gov_status": normalized["economic_governance_status"],
                    "soc_accept": {
                        "Love":        "soc_love",
                        "Save":        "soc_save",
                        "Indifferent": "soc_indiff",
                        "Dislike":     "soc_dislike",
                    }.get(normalized.get("Social Desirability", "").strip(), normalized.get("Social Desirability", "").strip()),
                    "sense": normalized["primary_sensory_perception"],
                    "rhythm": normalized["activity_rhythm"],
                }
            )
    return profiles


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def normalize_ai_system(ai_system: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(ai_system)
    # Ensure canonical keys exist regardless of source JSON naming.
    normalized["AI_System_ID"] = ai_system.get("AI_System_ID", ai_system.get("Use"))
    normalized["AI_System_Name"] = ai_system.get("AI_System_Name", ai_system.get("AI System Name", ai_system.get("Purpose", "Unknown AI System")))
    return normalized


def render_step1_prompt(template: str, ai_system: dict[str, Any], species_chunk: list[dict[str, str]]) -> str:
    prompt = template.replace("[INSERT AI SYSTEM JSON HERE]", json.dumps(ai_system, ensure_ascii=False, indent=2))
    prompt = prompt.replace("[INSERT ARRAY OF ORGANISM PROFILES HERE]", json.dumps(species_chunk, ensure_ascii=False, indent=2))
    return prompt


def render_step3_prompt(template: str, risk: dict[str, Any], species_profile: dict[str, str]) -> str:
    prompt = template.replace("[INSERT RISK JSON FROM STEP 2]", json.dumps(risk, ensure_ascii=False, indent=2))
    prompt = prompt.replace("[INSERT THE 10-DIMENSION PROFILE FOR THIS ORGANISM]", json.dumps(species_profile, ensure_ascii=False, indent=2))
    return prompt


def prepare_step1_requests(args: argparse.Namespace, env: dict[str, str]) -> None:
    require_args(args, ["species_csv", "ai_systems_json", "prompt_file", "species_batch_size", "model", "out_jsonl"])
    species = read_species_profiles(Path(args.species_csv))
    ai_systems = [normalize_ai_system(item) for item in read_json(Path(args.ai_systems_json))]
    prompt_template = Path(args.prompt_file).read_text(encoding="utf-8")
    species_batch_size = int(args.species_batch_size)

    requests: list[dict[str, Any]] = []
    for ai_idx, ai_system in enumerate(ai_systems):
        for chunk_idx, species_chunk in enumerate(chunked(species, species_batch_size)):
            custom_id = f"step1-ai{ai_idx:03d}-chunk{chunk_idx:03d}"
            requests.append(
                {
                    "custom_id": custom_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": args.model,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": "Output valid JSON only."},
                            {"role": "user", "content": render_step1_prompt(prompt_template, ai_system, species_chunk)},
                        ],
                    },
                }
            )

    out = Path(args.out_jsonl)
    out.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n", encoding="utf-8")
    print(f"Prepared {len(requests)} Step 1 requests -> {out}")
    print(f"AI systems: {len(ai_systems)} | Species: {len(species)} | Chunk size: {species_batch_size}")


def extract_json_from_text(content: str) -> Any:
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


def compile_step1_results(args: argparse.Namespace) -> None:
    require_args(args, ["results_jsonl", "out_json", "ai_systems_json"])
    aggregated: dict[str, list[dict[str, Any]]] = {}
    line_count = 0
    parsed_count = 0
    ai_systems = [normalize_ai_system(item) for item in read_json(Path(args.ai_systems_json))]
    custom_id_pattern = re.compile(r"step1-ai(\d+)-chunk(\d+)")
    risk_seq = 1

    with Path(args.results_jsonl).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            line_count += 1
            row = json.loads(line)
            response = row.get("response", {})
            body = response.get("body", {})
            choices = body.get("choices", [])
            if not choices:
                continue
            content = choices[0].get("message", {}).get("content", "")
            if not content:
                continue

            try:
                parsed = extract_json_from_text(content)
            except json.JSONDecodeError:
                continue

            if not isinstance(parsed, dict):
                continue
            parsed_count += 1
            custom_id = str(row.get("custom_id", ""))
            ai_idx_match = custom_id_pattern.search(custom_id)
            ai_system: dict[str, Any] | None = None
            if ai_idx_match:
                ai_idx = int(ai_idx_match.group(1))
                if 0 <= ai_idx < len(ai_systems):
                    ai_system = ai_systems[ai_idx]

            for species_name, risks in parsed.items():
                aggregated.setdefault(species_name, [])
                if isinstance(risks, list):
                    cleaned_risks: list[dict[str, Any]] = []
                    for risk in risks:
                        if not isinstance(risk, dict):
                            continue
                        normalized_risk = dict(risk)
                        # Harmonize organism/species naming for downstream steps.
                        organism_name = str(normalized_risk.get("Organism", species_name)).strip() or species_name
                        normalized_risk.setdefault("Species", organism_name)
                        normalized_risk.setdefault("Organism", organism_name)

                        # Force deterministic AI matching by ID from request custom_id.
                        if ai_system:
                            normalized_risk["AI_System_ID"] = ai_system.get("AI_System_ID")
                            normalized_risk["AI_System_Name"] = ai_system.get("AI_System_Name")
                            normalized_risk["AI_System"] = ai_system.get("AI_System_Name")
                        # Assign deterministic pipeline-wide unique risk id.
                        normalized_risk["Risk_ID"] = f"RISK-{risk_seq:07d}"
                        risk_seq += 1
                        cleaned_risks.append(normalized_risk)
                    aggregated[species_name].extend(cleaned_risks)

    write_json(Path(args.out_json), aggregated)
    print(f"Processed lines: {line_count}")
    print(f"Parsed JSON outputs: {parsed_count}")
    print(f"Wrote merged Step 1 output -> {args.out_json}")


def flatten_high_risks(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        risks: list[dict[str, Any]] = []
        for species_name, arr in payload.items():
            if isinstance(arr, list):
                for risk in arr:
                    if isinstance(risk, dict):
                        if "Species" not in risk:
                            risk["Species"] = species_name
                        risks.append(risk)
        return risks
    return []


def prepare_step3_requests(args: argparse.Namespace) -> None:
    require_args(args, ["species_csv", "high_risk_json", "prompt_file", "model", "out_jsonl"])
    species_profiles = read_species_profiles(Path(args.species_csv))
    species_map = {item["name"]: item for item in species_profiles}
    high_risk_payload = read_json(Path(args.high_risk_json))
    prompt_template = Path(args.prompt_file).read_text(encoding="utf-8")

    requests: list[dict[str, Any]] = []
    missing_species: set[str] = set()

    for idx, risk in enumerate(flatten_high_risks(high_risk_payload)):
        species_name = str(risk.get("Species", "")).strip()
        profile = species_map.get(species_name)
        if not profile:
            missing_species.add(species_name or "<missing>")
            continue
        risk_id = str(risk.get("Risk_ID", "")).strip() or f"RISK-MISSING-{idx:06d}"
        custom_id = f"step3-{risk_id}"
        requests.append(
            {
                "custom_id": custom_id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": args.model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "Output valid JSON only."},
                        {"role": "user", "content": render_step3_prompt(prompt_template, risk, profile)},
                    ],
                },
            }
        )

    Path(args.out_jsonl).write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n", encoding="utf-8")
    print(f"Prepared {len(requests)} Step 3 requests -> {args.out_jsonl}")
    if missing_species:
        print(f"Skipped {len(missing_species)} species not found in CSV: {sorted(missing_species)}")


def compile_step3_results(args: argparse.Namespace) -> None:
    require_args(args, ["results_jsonl", "out_json"])
    audits: list[dict[str, Any]] = []
    with Path(args.results_jsonl).open("r", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            response = row.get("response", {})
            body = response.get("body", {})
            choices = body.get("choices", [])
            if not choices:
                continue
            content = choices[0].get("message", {}).get("content", "")
            if not content:
                continue
            try:
                parsed = extract_json_from_text(content)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                custom_id = str(row.get("custom_id", ""))
                if custom_id.startswith("step3-RISK-"):
                    parsed["Risk_ID"] = custom_id.replace("step3-", "", 1)
                audits.append(parsed)

    write_json(Path(args.out_json), audits)
    print(f"Wrote {len(audits)} counterfactual audits -> {args.out_json}")


def submit_batch(args: argparse.Namespace, env: dict[str, str]) -> None:
    require_args(args, ["requests_jsonl", "completion_window"])
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Missing dependency: install with `pip install openai`.") from exc

    api_key = env.get("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is missing in .env or environment.")

    client = OpenAI(api_key=api_key)
    input_file = client.files.create(file=Path(args.requests_jsonl).open("rb"), purpose="batch")
    batch = client.batches.create(
        input_file_id=input_file.id,
        endpoint="/v1/chat/completions",
        completion_window=args.completion_window,
        metadata={"label": args.label},
    )
    print(f"Submitted batch: {batch.id}")
    print(f"Input file id : {input_file.id}")


def download_batch_output(args: argparse.Namespace, env: dict[str, str]) -> None:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("Missing dependency: install with `pip install openai`.") from exc

    api_key = env.get("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is missing in .env or environment.")

    client = OpenAI(api_key=api_key)
    batch = client.batches.retrieve(args.batch_id)
    if not batch.output_file_id:
        raise SystemExit(f"Batch {args.batch_id} has no output_file_id yet (status={batch.status}).")

    content = client.files.content(batch.output_file_id).text
    Path(args.out_jsonl).write_text(content, encoding="utf-8")
    print(f"Batch status: {batch.status}")
    print(f"Downloaded output -> {args.out_jsonl}")


def build_parser(env: dict[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-Ecological Risk Audit pipeline runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    step1 = sub.add_parser("prepare-step1", help="Build Step 1 Batch API request JSONL")
    step1.add_argument("--species-csv", default=env.get("SPECIES_CSV"))
    step1.add_argument("--ai-systems-json", default=env.get("AI_SYSTEMS_JSON"))
    step1.add_argument("--prompt-file", default=env.get("STEP1_PROMPT_FILE"))
    step1.add_argument("--species-batch-size", default=env.get("STEP1_SPECIES_BATCH_SIZE"))
    step1.add_argument("--model", default=env.get("STEP1_MODEL"))
    step1.add_argument("--out-jsonl", default=env.get("STEP1_REQUESTS_JSONL"))
    step1.set_defaults(func=prepare_step1_requests)

    compile1 = sub.add_parser("compile-step1", help="Merge Step 1 batch outputs into one JSON file")
    compile1.add_argument("--results-jsonl", default=env.get("STEP1_RESULTS_JSONL"))
    compile1.add_argument("--out-json", default=env.get("STEP1_OUTPUT_JSON"))
    compile1.add_argument("--ai-systems-json", default=env.get("AI_SYSTEMS_JSON"))
    compile1.set_defaults(func=lambda a, _env: compile_step1_results(a))

    step3 = sub.add_parser("prepare-step3", help="Build Step 3 Batch API request JSONL from filtered risks")
    step3.add_argument("--species-csv", default=env.get("SPECIES_CSV"))
    step3.add_argument("--high-risk-json", default=env.get("STEP2_HIGH_RISK_JSON"))
    step3.add_argument("--prompt-file", default=env.get("STEP3_PROMPT_FILE"))
    step3.add_argument("--model", default=env.get("STEP3_MODEL"))
    step3.add_argument("--out-jsonl", default=env.get("STEP3_REQUESTS_JSONL"))
    step3.set_defaults(func=lambda a, _env: prepare_step3_requests(a))

    compile3 = sub.add_parser("compile-step3", help="Merge Step 3 batch outputs into one JSON file")
    compile3.add_argument("--results-jsonl", default=env.get("STEP3_RESULTS_JSONL"))
    compile3.add_argument("--out-json", default=env.get("STEP3_OUTPUT_JSON"))
    compile3.set_defaults(func=lambda a, _env: compile_step3_results(a))

    submit = sub.add_parser("submit-batch", help="Upload request JSONL and submit batch")
    submit.add_argument("--requests-jsonl", required=True)
    submit.add_argument("--completion-window", default=env.get("BATCH_COMPLETION_WINDOW"))
    submit.add_argument("--label", default="risk-audit")
    submit.set_defaults(func=submit_batch)

    download = sub.add_parser("download-batch", help="Download completed batch output JSONL")
    download.add_argument("--batch-id", required=True)
    download.add_argument("--out-jsonl", required=True)
    download.set_defaults(func=download_batch_output)

    return parser


def main() -> None:
    env = load_env(ROOT / ".env")
    parser = build_parser(env)
    args = parser.parse_args()
    args.func(args, env)


if __name__ == "__main__":
    main()
