from models import SheetRow


def fetch_rows() -> list[dict]:
    """
    Fetch all data rows (excluding header) as list[dict] from Google Sheet.
    Uses the header row (A1:...) as keys.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    from config import settings

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(settings.google_creds_path, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(settings.google_sheets_id)
    ws = sh.worksheet(settings.google_sheets_worksheet)

    headers = ws.row_values(1)
    if not headers:
        return []

    # get_all_values includes header row, we skip it
    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        return []

    rows: list[dict] = []
    for vals in all_values[1:]:
        # pad / trim to header length
        padded = (vals + [""] * len(headers))[: len(headers)]
        rows.append(dict(zip(headers, padded)))
    return rows


def append_row(row: dict) -> None:
    """
    Append one row to Google Sheet. Requires service account creds JSON.
    """
    import gspread
    from google.oauth2.service_account import Credentials
    from gspread.exceptions import APIError

    from config import settings

    validated = SheetRow.model_validate(row)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(settings.google_creds_path, scopes=scopes)
    gc = gspread.authorize(creds)
    try:
        sh = gc.open_by_key(settings.google_sheets_id)
        ws = sh.worksheet(settings.google_sheets_worksheet)
    except APIError as e:
        # Surface HTTP status/message for faster debugging
        raise RuntimeError(f"gspread APIError: {getattr(e, 'response', None)} {e}") from e

    values = [
        validated.timestamp_utc_iso,
        str(validated.tg_user_id),
        validated.username or "",
        validated.full_name or "",
        validated.answers_json,
        validated.project_link,
        validated.project_note or "",
        validated.scores_json,
        str(validated.overall_score),
        "TRUE" if validated.top_candidate else "FALSE",
        validated.llm_model,
        str(validated.latency_ms),
        "TRUE" if validated.scoring_failed else "FALSE",
        validated.error or "",
    ]
    ws.append_row(values, value_input_option="RAW")
    return None
