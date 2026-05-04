import streamlit as st
import pandas as pd
import re
from io import BytesIO, StringIO
from collections import Counter

st.set_page_config(page_title="Wave Smart Rules Cleaner", layout="wide")
st.title("Wave Smart Rules Cleaner + Configurable Subledger Builder")

DEFAULT_RULES = pd.DataFrame([
    ["subledger", "shareholder loan", "Shareholder Loan"],
    ["subledger", "repay david", "Shareholder Loan"],
    ["subledger", "dcampbell", "Shareholder Loan"],
    ["subledger", "corp expenses to david", "Shareholder Loan"],
    ["subledger", "invoice payment", "AR"],
    ["subledger", "payment for invoice", "AR"],
    ["subledger", "aurora", "AR"],
    ["subledger", "subsidy", "AR"],
    ["subledger", "payroll", "Payroll"],
    ["subledger", "wagepoint", "Payroll"],
    ["subledger", "zensurance", "AP"],
    ["subledger", "professional services", "AP"],
    ["subledger", "rent", "AP"],
    ["subledger", "aws", "AP"],
    ["entity", "david", "David Campbell"],
    ["entity", "davidc", "David Campbell"],
    ["entity", "dcampbell", "David Campbell"],
    ["entity", "aurora", "Aurora"],
    ["entity", "zensurance", "Zensurance"],
    ["entity", "wagepoint", "Wagepoint"],
    ["entity", "cheng", "Cheng He"],
    ["entity", "deming", "DeMing Yao"],
    ["entity", "james ding", "James Ding"],
    ["category", "payroll", "Payroll"],
    ["category", "zensurance", "Insurance"],
    ["category", "aws", "Software / Cloud"],
    ["category", "rent", "Rent"],
    ["category", "professional services", "Professional Services"],
], columns=["rule_type", "keyword", "output"])


def money_to_float(x):
    if pd.isna(x):
        return 0.0
    x = str(x).strip().replace("$", "").replace(",", "")
    negative = x.startswith("(") and x.endswith(")")
    x = x.replace("(", "").replace(")", "")
    try:
        value = float(x) if x else 0.0
        return -value if negative else value
    except ValueError:
        return 0.0


def clean_text(x):
    return re.sub(r"\s+", " ", str(x).lower().strip())


def apply_rules(desc, rules, rule_type, default="Unknown"):
    d = clean_text(desc)
    subset = rules[rules["rule_type"] == rule_type]

    for _, r in subset.iterrows():
        keyword = clean_text(r["keyword"])
        if keyword and keyword in d:
            return r["output"], f"{rule_type}: '{keyword}'"

    return default, "No rule matched"


def classify_with_account(row, rules):
    desc = row["Description"]
    account = clean_text(row["Account Name"])
    debit = row["Debit"]
    credit = row["Credit"]

    subledger, rule_used = apply_rules(desc, rules, "subledger", "Unclassified")

    confidence = 0.50 if subledger != "Unclassified" else 0.25

    if "loan" in account or "shareholder" in account:
        subledger = "Shareholder Loan"
        confidence = max(confidence, 0.90)
        rule_used += " + account loan"

    elif "receivable" in account:
        subledger = "AR"
        confidence = max(confidence, 0.90)
        rule_used += " + account receivable"

    elif "payable" in account:
        subledger = "AP"
        confidence = max(confidence, 0.90)
        rule_used += " + account payable"

    elif any(x in account for x in ["cash", "bank", "rbc"]) and subledger == "Unclassified":
        subledger = "Cash / Bank"
        confidence = max(confidence, 0.65)
        rule_used += " + cash/bank account"

    d = clean_text(desc)

    if debit > 0 and any(x in d for x in ["invoice payment", "payment for invoice", "subsidy"]):
        subledger = "AR"
        confidence = max(confidence, 0.85)
        rule_used += " + debit receipt pattern"

    if credit > 0 and any(x in d for x in ["payroll", "rent", "zensurance", "professional services"]):
        subledger = "AP" if "payroll" not in d else "Payroll"
        confidence = max(confidence, 0.80)
        rule_used += " + credit expense pattern"

    return subledger, confidence, rule_used


def transaction_type(row):
    s = row["Subledger"]
    d = clean_text(row["Description"])

    if s == "Shareholder Loan":
        if any(x in d for x in ["repay", "repayment", "expenses to david", "corp expenses to david", "clearing"]):
            return "Repayment / Reimbursement"
        return "Loan Movement"

    if s == "AR":
        return "Customer Receipt / AR Movement"

    if s == "AP":
        return "Vendor Payment / AP Movement"

    if s == "Payroll":
        return "Payroll"

    if s == "Cash / Bank":
        return "Cash Inflow" if row["Debit"] > 0 else "Cash Outflow"

    return "Review"


def flow(row):
    if row["Debit"] > 0 and row["Credit"] == 0:
        return "Inflow / Debit"
    if row["Credit"] > 0 and row["Debit"] == 0:
        return "Outflow / Credit"
    if row["Debit"] > 0 and row["Credit"] > 0:
        return "Mixed"
    return "Zero / Unknown"


def flags(row):
    f = []

    if row["Subledger"] == "Unclassified":
        f.append("Unclassified")

    if row["Entity"] == "Unknown" and row["Subledger"] in ["AP", "AR", "Shareholder Loan"]:
        f.append("Unknown entity")

    if row["Confidence Score"] < 0.60:
        f.append("Low confidence")

    if row["Debit"] == 0 and row["Credit"] == 0:
        f.append("Zero amount")

    if row["Debit"] > 0 and row["Credit"] > 0:
        f.append("Both debit and credit populated")

    if abs(row["Amount"]) > 50000:
        f.append("Large transaction")

    return "; ".join(f) if f else "OK"


def read_wave_csv(file):
    raw = pd.read_csv(file, header=None, dtype=str)

    header_candidates = raw[
        raw.apply(
            lambda r: r.astype(str).str.contains("ACCOUNT NUMBER", case=False, na=False).any(),
            axis=1
        )
    ].index

    if len(header_candidates) == 0:
        st.error("Could not find the Wave header row with ACCOUNT NUMBER.")
        st.stop()

    header_row = header_candidates[0]
    file.seek(0)
    df = pd.read_csv(file, skiprows=header_row, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    current_account = None
    rows = []

    for _, row in df.iterrows():
        account_number = str(row.get("ACCOUNT NUMBER", "")).strip()
        date_value = str(row.get("DATE", "")).strip()
        desc = str(row.get("DESCRIPTION", "")).strip()

        if account_number == "" and date_value not in ["", "nan"] and desc in ["", "nan"]:
            current_account = date_value
            continue

        if "Starting Balance" in account_number:
            continue

        if date_value in ["", "nan"]:
            continue

        new_row = row.to_dict()
        new_row["Account Name"] = current_account
        rows.append(new_row)

    clean = pd.DataFrame(rows)

    clean["Date"] = pd.to_datetime(clean["DATE"], errors="coerce")
    clean["Description"] = clean["DESCRIPTION"].fillna("")
    clean["Account Number"] = clean["ACCOUNT NUMBER"].fillna("")
    clean["Debit"] = clean["DEBIT (In Business Currency)"].apply(money_to_float)
    clean["Credit"] = clean["CREDIT (In Business Currency)"].apply(money_to_float)
    clean["Balance"] = clean["BALANCE (In Business Currency)"].apply(money_to_float)
    clean["Amount"] = clean["Debit"] - clean["Credit"]

    return clean


def extract_keywords(df):
    stopwords = {
        "payment", "paid", "invoice", "for", "from", "with", "the", "and",
        "transfer", "created", "bill", "fee", "fees", "withdrawal",
        "deposit", "services", "professional", "account"
    }

    words = []
    for desc in df["Description"].fillna(""):
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", clean_text(desc))
        words.extend([t for t in tokens if t not in stopwords and len(t) > 2])

    return pd.DataFrame(Counter(words).most_common(100), columns=["keyword", "frequency"])


def suggest_rules(df):
    suggestions = []

    unknowns = df[df["Subledger"].isin(["Unclassified", "Cash / Bank"])].copy()
    keywords = extract_keywords(unknowns)

    for _, row in keywords.head(50).iterrows():
        kw = row["keyword"]
        freq = row["frequency"]

        suggested = "AP"
        if kw in ["aurora", "mcmaster", "subsidy", "obio", "kawartha"]:
            suggested = "AR"
        elif kw in ["payroll", "wagepoint"]:
            suggested = "Payroll"
        elif kw in ["david", "davidc", "dcampbell", "shareholder"]:
            suggested = "Shareholder Loan"

        suggestions.append(["subledger", kw, suggested, freq, "Suggested from unknown keyword frequency"])

    return pd.DataFrame(
        suggestions,
        columns=["rule_type", "keyword", "output", "frequency", "reason"]
    )


def process(df, rules):
    classified = df.apply(lambda r: classify_with_account(r, rules), axis=1)

    df["Subledger"] = classified.apply(lambda x: x[0])
    df["Confidence Score"] = classified.apply(lambda x: x[1])
    df["Rule Used"] = classified.apply(lambda x: x[2])

    df["Entity"] = df["Description"].apply(lambda x: apply_rules(x, rules, "entity", "Unknown")[0])
    df["Category"] = df["Description"].apply(lambda x: apply_rules(x, rules, "category", "Unclassified")[0])

    df["Transaction Type"] = df.apply(transaction_type, axis=1)
    df["Flow"] = df.apply(flow, axis=1)
    df["Subledger Detail"] = df["Subledger"] + " - " + df["Entity"]
    df["Needs Review"] = df["Confidence Score"] < 0.60
    df["Flag"] = df.apply(flags, axis=1)

    return df


def to_excel(df, suggestions):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Clean Transactions", index=False)
        df[df["Flag"] != "OK"].to_excel(writer, sheet_name="Red Flags", index=False)

        df.groupby("Subledger").agg(
            Transactions=("Description", "count"),
            Total_Amount=("Amount", "sum"),
            Avg_Confidence=("Confidence Score", "mean"),
            Red_Flags=("Flag", lambda x: (x != "OK").sum())
        ).reset_index().to_excel(writer, sheet_name="Summary Subledger", index=False)

        df.groupby(["Subledger", "Entity"]).agg(
            Transactions=("Description", "count"),
            Total_Amount=("Amount", "sum"),
            Avg_Confidence=("Confidence Score", "mean"),
            Red_Flags=("Flag", lambda x: (x != "OK").sum())
        ).reset_index().to_excel(writer, sheet_name="Summary Entity", index=False)

        extract_keywords(df).to_excel(writer, sheet_name="Keyword Discovery", index=False)
        suggestions.to_excel(writer, sheet_name="Suggested Rules", index=False)

    output.seek(0)
    return output


def df_to_csv_download(df):
    return df.to_csv(index=False).encode("utf-8")


st.sidebar.header("Inputs")

wave_file = st.sidebar.file_uploader("1. Upload Wave CSV", type=["csv"], key="wave")
rules_file = st.sidebar.file_uploader("2. Optional: Upload rules_config.csv", type=["csv"], key="rules")

st.sidebar.download_button(
    "Download rules_config template",
    data=df_to_csv_download(DEFAULT_RULES),
    file_name="rules_config_template.csv",
    mime="text/csv"
)

if wave_file:
    rules = DEFAULT_RULES.copy()

    if rules_file:
        rules = pd.read_csv(rules_file)
        expected = {"rule_type", "keyword", "output"}
        if not expected.issubset(set(rules.columns)):
            st.error("rules_config.csv must have columns: rule_type, keyword, output")
            st.stop()

    raw_df = read_wave_csv(wave_file)
    processed = process(raw_df.copy(), rules)
    suggestions = suggest_rules(processed)

    st.success("CSV processed.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Transactions", len(processed))
    c2.metric("Red Flags", int((processed["Flag"] != "OK").sum()))
    c3.metric("Unclassified", int((processed["Subledger"] == "Unclassified").sum()))
    c4.metric("Avg Confidence", round(processed["Confidence Score"].mean(), 2))

    st.subheader("Clean Transactions")
    st.dataframe(processed.head(300), use_container_width=True)

    st.subheader("Red Flags")
    st.dataframe(processed[processed["Flag"] != "OK"], use_container_width=True)

    st.subheader("Suggested Rules")
    st.write("Review these. Add the useful ones to your rules_config.csv and re-upload it.")
    st.dataframe(suggestions, use_container_width=True)

    st.download_button(
        "Download suggested_rules.csv",
        data=df_to_csv_download(suggestions[["rule_type", "keyword", "output"]]),
        file_name="suggested_rules.csv",
        mime="text/csv"
    )

    excel = to_excel(processed, suggestions)

    st.download_button(
        "Download cleaned Excel output",
        data=excel,
        file_name="wave_cleaned_with_subledgers.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

else:
    st.info("Upload your Wave CSV to begin.")
