import argparse
import json
import random
from pathlib import Path
from typing import Any


def filter_risks(input_filepath: str, high_risk_output: str, low_risk_output: str) -> None:
    input_path = Path(input_filepath)
    if not input_path.exists():
        print(f"Error: The file {input_filepath} was not found.")
        return

    data: Any = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("Error: Step 1 input must be a JSON object in the shape {species_name: [risks...]}.")
        return

    # Flatten all risks and compute SxF score
    all_risks: list[tuple[str, dict[str, Any], int]] = []
    for species_name, risks_array in data.items():
        if not isinstance(risks_array, list):
            continue
        for risk in risks_array:
            if not isinstance(risk, dict):
                continue
            try:
                severity = int(risk.get("Severity", 0))
                frequency = int(risk.get("Frequency", 0))
            except (ValueError, TypeError):
                print(f"Warning: Bad Severity/Frequency under '{species_name}', risk skipped.")
                continue
            all_risks.append((species_name, risk, severity * frequency))

    if not all_risks:
        print("No valid risks found.")
        return

    # Determine top 10% quota and boundary score
    all_risks.sort(key=lambda x: x[2], reverse=True)
    quota = max(1, len(all_risks) // 10)
    boundary_score = all_risks[quota - 1][2]
    cutoff_score = boundary_score + 1  # guaranteed-in threshold

    # Partition into: above cutoff, at boundary, below boundary
    above   = [(s, r, sc) for s, r, sc in all_risks if sc >= cutoff_score]
    at_boundary = [(s, r, sc) for s, r, sc in all_risks if sc == boundary_score]
    below   = [(s, r, sc) for s, r, sc in all_risks if sc < boundary_score]

    # Fill remaining quota slots with a random sample from boundary-score risks
    remaining_slots = max(0, quota - len(above))
    randomly_selected = random.sample(at_boundary, min(remaining_slots, len(at_boundary)))
    randomly_selected_set = set(id(r) for _, r, _ in randomly_selected)

    high_risk_data: dict[str, list[dict[str, Any]]] = {k: [] for k in data}
    low_risk_data: dict[str, list[dict[str, Any]]] = {k: [] for k in data}

    for species_name, risk, score in above + randomly_selected:
        r = dict(risk)
        r["Risk_Score"] = score
        high_risk_data[species_name].append(r)

    for species_name, risk, score in below:
        r = dict(risk)
        r["Risk_Score"] = score
        low_risk_data[species_name].append(r)

    # Boundary risks not selected go to low
    for species_name, risk, score in at_boundary:
        if id(risk) not in randomly_selected_set:
            r = dict(risk)
            r["Risk_Score"] = score
            low_risk_data[species_name].append(r)

    high_count = sum(len(v) for v in high_risk_data.values())
    low_count  = sum(len(v) for v in low_risk_data.values())

    Path(high_risk_output).write_text(json.dumps(high_risk_data, indent=2, ensure_ascii=False), encoding="utf-8")
    Path(low_risk_output).write_text(json.dumps(low_risk_data, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=== FILTERING COMPLETE ===")
    print(f"Total Risks Evaluated : {len(all_risks)}")
    print(f"Quota (10%)           : {quota}")
    print(f"Guaranteed in (>={cutoff_score})  : {len(above)}")
    print(f"Boundary score ({boundary_score})    : {len(at_boundary)} total, {len(randomly_selected)} randomly selected")
    print(f"High Risks            : {high_count} -> {high_risk_output}")
    print(f"Low Risks             : {low_count} -> {low_risk_output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Filter Step 1 risks: top 25% by Severity x Frequency.")
    parser.add_argument("--input", default="step1_llm_output.json")
    parser.add_argument("--high-out", default="step2_high_risk_filtered.json")
    parser.add_argument("--low-out", default="step2_low_risk_appendix.json")
    args = parser.parse_args()

    filter_risks(args.input, args.high_out, args.low_out)


if __name__ == "__main__":
    main()
