#!/usr/bin/env python3
"""
Pipeline DQA Kobo -> Neon (PostgreSQL) — version script, sans interface.

Reprend exactement les étapes 3 à 7 du notebook Colab :
  3. Récupération du formulaire Kobo + génération des règles + dictionnaire des labels
  4. Récupération des soumissions + nettoyage des noms de colonnes
  5. Moteur DQA (complétude, validité, doublons, valeurs aberrantes)
  6. (dashboard visuel ignoré ici — pas d'écran en exécution planifiée)
  7. Envoi vers Neon (submissions, dqa_runs, dqa_results, dqa_issues, dictionary)

Toute la configuration passe par des VARIABLES D'ENVIRONNEMENT (jamais de input()/getpass()) :
  KOBO_SERVER         ex: kf.kobotoolbox.org
  KOBO_ASSET_UID      ex: aAbBcCdD1234...
  KOBO_API_TOKEN      jeton API Kobo
  NEON_DATABASE_URL   chaîne de connexion Neon (postgresql://user:pass@host/db)
  DUPLICATE_SUBSET    (optionnel) colonnes séparées par des virgules pour détecter les doublons,
                       défaut: "s0_3"

Utilisation locale :
  export KOBO_SERVER=kf.kobotoolbox.org
  export KOBO_ASSET_UID=...
  export KOBO_API_TOKEN=...
  export NEON_DATABASE_URL=postgresql://...
  python dqa_kobo_to_neon.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import numpy as np
import pandas as pd
import requests
from sqlalchemy import create_engine, text


# --------------------------------------------------------------------------
# 0. Configuration — lue depuis les variables d'environnement uniquement
# --------------------------------------------------------------------------
def get_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"❌ Variable d'environnement manquante : {name}", file=sys.stderr)
        sys.exit(1)
    return value


KOBO_SERVER = get_required_env("KOBO_SERVER")
ASSET_UID = get_required_env("KOBO_ASSET_UID")
API_TOKEN = get_required_env("KOBO_API_TOKEN")
NEON_DATABASE_URL = get_required_env("NEON_DATABASE_URL")
DUPLICATE_SUBSET = [c.strip() for c in os.environ.get("DUPLICATE_SUBSET", "s0_3").split(",") if c.strip()]
# Colonne identifiant l'enquêteur : par défaut le champ Kobo "_submitted_by"
# (rempli automatiquement si la soumission est authentifiée). Si ton formulaire
# a plutôt une question dédiée (ex: "nom_enqueteur"), mets-la dans cette variable.
ENUMERATOR_COL = os.environ.get("ENUMERATOR_COL", "_submitted_by").strip()
# Champs système Kobo à ne JAMAIS compter comme "manquants" (métadonnées techniques,
# toujours présentes ou légitimement vides, ne relèvent pas de la qualité de la collecte).
SYSTEM_COLUMNS = {
    "_id", "_uuid", "_submission_time", "_submitted_by", "_validation_status",
    "_notes", "_status", "_tags", "_index", "_xform_id_string", "_attachments",
    "_geolocation", "_supplementary_details", "__version__", "formhub_uuid",
    "meta_instanceid", "start", "end", "today", "deviceid", "phonenumber",
    "username", "simserial", "subscriberid", "imei", "audit",
}
# Colonnes supplémentaires à exclure du contrôle de complétude, propres à votre formulaire
# (questions à logique conditionnelle légitimement vides, groupes de répétition sérialisés...)
EXCLUDE_MISSING_COLS = {c.strip() for c in os.environ.get("EXCLUDE_MISSING_COLS", "").split(",") if c.strip()}

# Tolère un secret KOBO_SERVER déjà préfixé par http(s):// (évite le bug "https://https://...")
KOBO_SERVER = re.sub(r"^https?://", "", KOBO_SERVER).rstrip("/")
BASE_URL = f"https://{KOBO_SERVER}"

# Neon exige SSL — on force sslmode=require si absent de l'URL
parsed = urlparse(NEON_DATABASE_URL)
qs = parse_qs(parsed.query)
if "sslmode" not in qs:
    sep = "&" if parsed.query else "?"
    NEON_DATABASE_URL = f"{NEON_DATABASE_URL}{sep}sslmode=require"

print(f"✅ Configuré pour {BASE_URL} / asset {ASSET_UID}")


# --------------------------------------------------------------------------
# 3. Récupération du formulaire + génération des règles + dictionnaire
# --------------------------------------------------------------------------
def kobo_get(url, token):
    headers = {"Authorization": f"Token {token}"}
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_survey_structure(base_url, uid, token):
    url = f"{base_url}/api/v2/assets/{uid}/?format=json"
    data = kobo_get(url, token)
    return data.get("content", {}).get("survey", []), data.get("name", uid)


def short_name(name: str) -> str:
    name = str(name).split("/")[-1]
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()


def parse_constraint(constraint):
    constraint = constraint.strip()
    m = re.match(r"regex\(\.\s*,\s*\'([^\']*)\'\)", constraint)
    if m:
        return {"type": "regex", "pattern": m.group(1)}
    if "${" not in constraint:
        min_match = re.search(r"\.\s*>=\s*(-?\d+\.?\d*)", constraint)
        max_match = re.search(r"\.\s*<=\s*(-?\d+\.?\d*)", constraint)
        if min_match or max_match:
            rule = {"type": "numeric"}
            if min_match:
                rule["min"] = float(min_match.group(1))
            if max_match:
                rule["max"] = float(max_match.group(1))
            return rule
    return {"type": "manual_review", "expression": constraint}


def generate_rules(survey_structure):
    rules, manual_review = {}, {}
    for row in survey_structure:
        constraint = str(row.get("constraint", "") or "").strip()
        name = row.get("name") or row.get("$autoname", "")
        if not constraint or not name:
            continue
        clean_name = short_name(name)
        parsed_rule = parse_constraint(constraint)
        if parsed_rule["type"] == "manual_review":
            manual_review[clean_name] = parsed_rule["expression"]
        else:
            rules[clean_name] = parsed_rule
    return rules, manual_review


def _first_label(label_field):
    if isinstance(label_field, list):
        for l in label_field:
            if l:
                return str(l)
        return ""
    return str(label_field or "")


def build_label_dictionary(survey_structure):
    labels = {}
    for row in survey_structure:
        name = row.get("name") or row.get("$autoname", "")
        if not name:
            continue
        clean = short_name(name)
        label = _first_label(row.get("label"))
        qtype = row.get("type", "")
        if clean not in labels or (label and not labels[clean][0]):
            labels[clean] = (label, qtype)
    return labels


# --------------------------------------------------------------------------
# 4. Récupération des soumissions
# --------------------------------------------------------------------------
def fetch_all_submissions(base_url, uid, token, page_size=1000):
    all_results = []
    url = f"{base_url}/api/v2/assets/{uid}/data/?format=json&limit={page_size}"
    while url:
        data = kobo_get(url, token)
        all_results.extend(data.get("results", []))
        url = data.get("next")
    return all_results


# --------------------------------------------------------------------------
# 5. Moteur DQA
# --------------------------------------------------------------------------
def check_numeric(series, min_value=None, max_value=None):
    errors = pd.Series(False, index=series.index)
    numeric = pd.to_numeric(series, errors="coerce")
    if min_value is not None:
        errors |= numeric < min_value
    if max_value is not None:
        errors |= numeric > max_value
    return errors


def check_regex(series, pattern):
    compiled = re.compile(pattern)

    def _is_invalid(value):
        # pd.isna gère tous les types de "manquant" (NaN, None, NaT...) avant toute conversion
        if pd.isna(value):
            return False  # valeur manquante -> pas considérée en erreur de format ici
        text = str(value)
        if text in ("", "nan", "None"):
            return False
        return not bool(compiled.match(text))

    return series.apply(_is_invalid)


def check_missing(df):
    return df.isna() | (df.astype(str).apply(lambda c: c.str.strip()) == "")


def check_duplicates(df, subset):
    subset = [c for c in subset if c in df.columns]
    if not subset:
        return pd.Series(False, index=df.index)
    return df.duplicated(subset=subset, keep=False)


def check_outliers_iqr(series):
    numeric = pd.to_numeric(series, errors="coerce")
    q1, q3 = numeric.quantile(0.25), numeric.quantile(0.75)
    iqr = q3 - q1
    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return (numeric < low) | (numeric > high)


def run_dqa(df: pd.DataFrame, rules: dict, duplicate_subset=None, uuid_col=None, enumerator_col=None):
    n_obs = len(df)
    results_rows, issues_rows = [], []

    def _enqueteur(idx):
        if enumerator_col and enumerator_col in df.columns:
            val = df.at[idx, enumerator_col]
            return None if pd.isna(val) else str(val)
        return None

    # --- 5.1 Validité (regex / bornes numériques déjà déclarées dans le xlsform) ---
    for variable, rule in rules.items():
        if variable not in df.columns:
            continue
        if rule["type"] == "numeric":
            errors = check_numeric(df[variable], rule.get("min"), rule.get("max"))
        elif rule["type"] == "regex":
            errors = check_regex(df[variable], rule["pattern"])
        else:
            continue

        n_errors = int(errors.sum())
        results_rows.append({
            "variable": variable, "controle": "validite",
            "erreurs": n_errors, "taux": round(100 * n_errors / n_obs, 2) if n_obs else 0,
        })
        for idx in df.index[errors]:
            issues_rows.append({
                "row": idx,
                "submission_uuid": df.at[idx, uuid_col] if uuid_col else None,
                "enqueteur": _enqueteur(idx),
                "variable": variable, "type": "valeur_invalide",
                "valeur": df.at[idx, variable],
            })

    # --- 5.2 Complétude (valeurs manquantes) : journalisée variable par variable ---
    # On exclut les champs système Kobo et les colonnes de type liste/dict (repeat groups
    # déjà sérialisés en JSON) qui n'ont pas de sens en "manquant/pas manquant" simple.
    missing = check_missing(df)
    checked_cols = [
        c for c in df.columns
        if c not in SYSTEM_COLUMNS and c not in EXCLUDE_MISSING_COLS
        and not df[c].map(lambda v: isinstance(v, (list, dict))).any()
    ]
    completeness = 100 * (1 - missing[checked_cols].mean().mean()) if checked_cols else 100
    for variable in checked_cols:
        col_missing = missing[variable]
        n_missing = int(col_missing.sum())
        if n_missing == 0:
            continue
        results_rows.append({
            "variable": variable, "controle": "completude",
            "erreurs": n_missing, "taux": round(100 * n_missing / n_obs, 2) if n_obs else 0,
        })
        for idx in df.index[col_missing]:
            issues_rows.append({
                "row": idx,
                "submission_uuid": df.at[idx, uuid_col] if uuid_col else None,
                "enqueteur": _enqueteur(idx),
                "variable": variable, "type": "valeur_manquante",
                "valeur": None,
            })

    # --- 5.3 Doublons : journalisés ligne par ligne ---
    if duplicate_subset:
        dup_mask = check_duplicates(df, duplicate_subset)
        n_dup = int(dup_mask.sum())
        if n_dup:
            for idx in df.index[dup_mask]:
                issues_rows.append({
                    "row": idx,
                    "submission_uuid": df.at[idx, uuid_col] if uuid_col else None,
                    "enqueteur": _enqueteur(idx),
                    "variable": "+".join(duplicate_subset), "type": "doublon",
                    "valeur": " | ".join(str(df.at[idx, c]) for c in duplicate_subset if c in df.columns),
                })
    else:
        n_dup = 0

    # --- 5.4 Valeurs aberrantes (IQR) : journalisées ligne par ligne ---
    n_outliers = 0
    for variable, rule in rules.items():
        if rule["type"] == "numeric" and variable in df.columns:
            out_mask = check_outliers_iqr(df[variable])
            n_out = int(out_mask.sum())
            n_outliers += n_out
            if n_out:
                results_rows.append({
                    "variable": variable, "controle": "aberrant",
                    "erreurs": n_out, "taux": round(100 * n_out / n_obs, 2) if n_obs else 0,
                })
                for idx in df.index[out_mask]:
                    issues_rows.append({
                        "row": idx,
                        "submission_uuid": df.at[idx, uuid_col] if uuid_col else None,
                        "enqueteur": _enqueteur(idx),
                        "variable": variable, "type": "valeur_aberrante",
                        "valeur": df.at[idx, variable],
                    })

    validity_rows = [r for r in results_rows if r["controle"] == "validite"]
    validity_errors = sum(r["erreurs"] for r in validity_rows)
    n_vars_checked = len(validity_rows)
    validity_score = 100 * (1 - validity_errors / (n_obs * n_vars_checked)) if n_obs and n_vars_checked else 100

    global_score = round((completeness + validity_score) / 2, 1)
    status = "EXCELLENT" if global_score >= 95 else "BON" if global_score >= 85 else "A AMELIORER"

    return {
        "run_date": datetime.now(timezone.utc),
        "n_obs": int(n_obs),
        "n_vars_checked": int(n_vars_checked),
        "n_vars_completeness_checked": int(len(checked_cols)),
        "completeness_score": float(round(completeness, 2)),
        "validity_score": float(round(validity_score, 2)),
        "global_score": float(global_score),
        "status": status,
        "n_duplicates": int(n_dup),
        "n_outliers": int(n_outliers),
        "results": pd.DataFrame(results_rows),
        "issues": pd.DataFrame(issues_rows),
    }


# --------------------------------------------------------------------------
# 6bis. Sérialisation des colonnes "repeat group" (listes/dicts) en JSON texte
#       avant l'envoi vers Neon — PostgreSQL ne sait pas stocker un objet
#       Python brut dans une colonne.
# --------------------------------------------------------------------------
def _jsonify_value(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def prepare_df_for_sql(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    n_converted = 0
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, (list, dict))).any():
            df[col] = df[col].map(_jsonify_value)
            n_converted += 1
    if n_converted:
        print(f"ℹ️ {n_converted} colonne(s) de type répétition (listes/dicts) converties en JSON texte")
    return df


# --------------------------------------------------------------------------
# 7. Envoi vers Neon
# --------------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS dqa_runs (
    run_id SERIAL PRIMARY KEY,
    asset_uid TEXT,
    form_name TEXT,
    run_date TIMESTAMPTZ,
    n_obs INTEGER,
    n_vars_checked INTEGER,
    completeness_score NUMERIC,
    validity_score NUMERIC,
    global_score NUMERIC,
    status TEXT,
    n_duplicates INTEGER,
    n_outliers INTEGER
);

CREATE TABLE IF NOT EXISTS dqa_results (
    id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES dqa_runs(run_id),
    variable TEXT,
    controle TEXT,
    erreurs INTEGER,
    error_rate NUMERIC
);

CREATE TABLE IF NOT EXISTS dqa_issues (
    id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES dqa_runs(run_id),
    submission_uuid TEXT,
    row_id INTEGER,
    enqueteur TEXT,
    variable TEXT,
    type TEXT,
    valeur TEXT
);

-- Si la table existe déjà (déploiement précédent), exécuter une fois :
-- ALTER TABLE dqa_issues ADD COLUMN IF NOT EXISTS enqueteur TEXT;

CREATE TABLE IF NOT EXISTS dictionary (
    variable TEXT PRIMARY KEY,
    label TEXT,
    type TEXT,
    chemin_kobo TEXT
);
"""


def main():
    # --- Étape 3 ---
    survey_structure, form_name = fetch_survey_structure(BASE_URL, ASSET_UID, API_TOKEN)
    rules, manual_review = generate_rules(survey_structure)
    label_dictionary = build_label_dictionary(survey_structure)
    print(f'✅ Formulaire : "{form_name}"')
    print(f"✅ {len(rules)} règles générées, {len(manual_review)} à vérifier manuellement")
    print(f"✅ {len(label_dictionary)} libellés de questions récupérés")

    # --- Étape 4 ---
    submissions_raw = fetch_all_submissions(BASE_URL, ASSET_UID, API_TOKEN)
    df = pd.json_normalize(submissions_raw)

    rename_map, seen = {}, set()
    for col in df.columns:
        base = short_name(col)
        candidate = base
        if candidate in seen:
            parts = [p for p in col.split("/") if p]
            if len(parts) >= 2:
                candidate = short_name(f"{parts[-2]}_{parts[-1]}")
        n = 2
        while candidate in seen:
            candidate = f"{base}_{n}"
            n += 1
        rename_map[col] = candidate
        seen.add(candidate)
    df = df.rename(columns=rename_map)

    # Filet de sécurité : si deux colonnes finissent quand même avec le même nom
    # (cas limite avec un très gros formulaire), on les distingue avant d'aller plus loin.
    if df.columns.duplicated().any():
        cols = pd.Series(df.columns)
        for dup in cols[cols.duplicated()].unique():
            idxs = cols[cols == dup].index
            for i, idx in enumerate(idxs[1:], start=2):
                cols[idx] = f"{dup}_{i}"
        df.columns = cols
        print(f"⚠️ {len(idxs)} colonnes en doublon détectées et renommées automatiquement ({dup}...)")

    dictionary_rows = []
    for original_col, clean_col in rename_map.items():
        label, qtype = label_dictionary.get(short_name(original_col), ("", ""))
        dictionary_rows.append({
            "variable": clean_col,
            "label": label if label else clean_col,
            "type": qtype,
            "chemin_kobo": original_col,
        })
    dictionary_df = pd.DataFrame(dictionary_rows)

    uuid_col = "_uuid" if "_uuid" in df.columns else None
    print(f"✅ {len(df)} soumissions, {len(df.columns)} colonnes")

    # --- Étape 5 ---
    enumerator_col = ENUMERATOR_COL if ENUMERATOR_COL in df.columns else None
    if ENUMERATOR_COL and not enumerator_col:
        print(f"⚠️ Colonne enquêteur '{ENUMERATOR_COL}' absente des soumissions — le champ 'enqueteur' de dqa_issues restera vide.")
    report = run_dqa(df, rules, duplicate_subset=DUPLICATE_SUBSET, uuid_col=uuid_col, enumerator_col=enumerator_col)
    print(f"""
================================
       DATA QUALITY REPORT
================================
Formulaire            : {form_name}
Observations           : {report['n_obs']}
Variables contrôlées    : {report['n_vars_checked']}
Complétude              : {report['completeness_score']} %
Validité                : {report['validity_score']} %
Doublons détectés       : {report['n_duplicates']}
Valeurs aberrantes      : {report['n_outliers']}
--------------------------------
SCORE GLOBAL            : {report['global_score']} %
--------------------------------
Statut : {report['status']}
""")
    print(f"ℹ️ {len(report['issues'])} anomalies au total à insérer dans dqa_issues "
          f"(variables contrôlées pour la complétude : {report['n_vars_completeness_checked']})")

    # --- Étape 7 ---
    conn_str = NEON_DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(conn_str, pool_pre_ping=True)

    with engine.begin() as conn:
        conn.execute(text(DDL))
        # Filet de sécurité : si dqa_issues existait déjà (déploiement précédent, sans la
        # colonne enqueteur), on l'ajoute ici automatiquement plutôt que de dépendre d'une
        # étape manuelle sur Neon.
        conn.execute(text("ALTER TABLE dqa_issues ADD COLUMN IF NOT EXISTS enqueteur TEXT;"))

    with engine.begin() as conn:
        result = conn.execute(text("""
            INSERT INTO dqa_runs
                (asset_uid, form_name, run_date, n_obs, n_vars_checked,
                 completeness_score, validity_score, global_score, status,
                 n_duplicates, n_outliers)
            VALUES
                (:asset_uid, :form_name, :run_date, :n_obs, :n_vars_checked,
                 :completeness_score, :validity_score, :global_score, :status,
                 :n_duplicates, :n_outliers)
            RETURNING run_id
        """), {
            "asset_uid": ASSET_UID, "form_name": form_name, "run_date": report["run_date"],
            "n_obs": report["n_obs"], "n_vars_checked": report["n_vars_checked"],
            "completeness_score": report["completeness_score"], "validity_score": report["validity_score"],
            "global_score": report["global_score"], "status": report["status"],
            "n_duplicates": report["n_duplicates"], "n_outliers": report["n_outliers"],
        })
        run_id = result.scalar()

    results_df = report["results"].copy()
    if not results_df.empty:
        results_df["run_id"] = run_id
        results_df["error_rate"] = results_df["taux"] / 100
        results_df[["run_id", "variable", "controle", "erreurs", "error_rate"]].to_sql(
            "dqa_results", engine, if_exists="append", index=False)

    issues_df = report["issues"].copy()
    if not issues_df.empty:
        issues_df["run_id"] = run_id
        issues_df = issues_df.rename(columns={"row": "row_id"})
        issues_df["valeur"] = issues_df["valeur"].astype(str).replace({"None": None, "nan": None})
        issues_df[["run_id", "submission_uuid", "row_id", "enqueteur", "variable", "type", "valeur"]].to_sql(
            "dqa_issues", engine, if_exists="append", index=False, chunksize=500, method="multi")

    df = prepare_df_for_sql(df)
    df.to_sql("submissions", engine, if_exists="replace", index=False)
    dictionary_df.to_sql("dictionary", engine, if_exists="replace", index=False)

    print(f"✅ Run #{run_id} envoyé vers Neon")
    print(f"✅ {len(results_df)} lignes dans dqa_results, {len(issues_df)} lignes dans dqa_issues")
    print(f"✅ Table submissions mise à jour ({len(df)} lignes)")
    print(f"✅ Table dictionary mise à jour ({len(dictionary_df)} variables)")


if __name__ == "__main__":
    main()
