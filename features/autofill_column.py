import io
import re
from pathlib import Path

import pandas as pd
import streamlit as st
from googleapiclient.discovery import build

from features.auth import get_credentials, require_auth


SUPPORTED_UPLOAD_TYPES = ["csv", "xls", "xlsx"]


def render():
    st.header("Autofill Excel / Google Sheets Column")
    st.caption(
        "Fill blank cells in one column using the last value above, stopping when the limit column is blank."
    )

    source = st.radio(
        "Data source",
        ["Upload Excel / CSV", "Google Sheets"],
        horizontal=True,
    )

    if source == "Upload Excel / CSV":
        _render_upload_flow()
    else:
        _render_google_sheets_flow()


def autofill_column(df: pd.DataFrame, column_to_fill: str, limit_column: str) -> pd.DataFrame:
    if column_to_fill not in df.columns:
        raise ValueError(f"Column '{column_to_fill}' not found.")
    if limit_column not in df.columns:
        raise ValueError(f"Column '{limit_column}' not found.")

    result = df.copy()
    output_column = f"{column_to_fill}_autofilled"
    result[output_column] = result[column_to_fill].replace("", pd.NA)

    last_value = pd.NA
    for i in range(len(result)):
        current_value = result.loc[i, column_to_fill]
        limit_value = result.loc[i, limit_column]

        if _has_value(current_value):
            last_value = current_value
            result.loc[i, output_column] = current_value
        elif _has_value(limit_value) and _has_value(last_value):
            result.loc[i, output_column] = last_value
        elif not _has_value(limit_value):
            last_value = pd.NA

    return result


def _render_upload_flow():
    uploaded = st.file_uploader(
        "Upload Excel or CSV file",
        type=SUPPORTED_UPLOAD_TYPES,
        help="Supported formats: .xlsx, .xls, .csv",
    )
    if uploaded is None:
        _render_quick_guide()
        return

    try:
        df = _load_uploaded_dataframe(uploaded)
    except Exception as exc:
        st.error(f"Could not read file: {exc}")
        return

    _render_dataframe_autofill(df, uploaded.name)


def _render_google_sheets_flow():
    st.info(
        "Google Sheets needs Google authentication with Sheets permission. "
        "If this fails, sign out in Settings and authenticate again."
    )
    if not require_auth():
        return

    sheet_url = st.text_input(
        "Google Sheets URL or spreadsheet ID",
        placeholder="https://docs.google.com/spreadsheets/d/...",
    )
    worksheet = st.text_input("Sheet tab name", value="Sheet1")

    if st.button("Load Sheet", type="primary"):
        spreadsheet_id = _extract_spreadsheet_id(sheet_url)
        if not spreadsheet_id:
            st.error("Please enter a valid Google Sheets URL or spreadsheet ID.")
            return

        try:
            service = _sheets_service()
            df = _load_sheet_dataframe(service, spreadsheet_id, worksheet)
        except Exception as exc:
            st.error(f"Could not load Google Sheet: {exc}")
            return

        st.session_state.autofill_sheet = {
            "spreadsheet_id": spreadsheet_id,
            "worksheet": worksheet,
            "df": df,
        }
        st.session_state.pop("autofill_result", None)
        st.session_state.pop("autofill_output_column", None)

    sheet_state = st.session_state.get("autofill_sheet")
    if not sheet_state:
        _render_quick_guide()
        return

    result = _render_dataframe_autofill(
        sheet_state["df"],
        f"{sheet_state['worksheet']}.csv",
        allow_download=False,
    )

    output_column = st.session_state.get("autofill_output_column")
    saved_result = st.session_state.get("autofill_result")
    if output_column and saved_result is not None and st.button("Write autofilled column to Google Sheet"):
        try:
            service = _sheets_service()
            _write_sheet_column(
                service,
                sheet_state["spreadsheet_id"],
                sheet_state["worksheet"],
                output_column,
                saved_result[output_column],
            )
            st.success(f"Updated Google Sheet column: {output_column}")
        except Exception as exc:
            st.error(f"Could not update Google Sheet: {exc}")


def _render_dataframe_autofill(
    df: pd.DataFrame,
    filename: str,
    allow_download: bool = True,
) -> pd.DataFrame | None:
    if df.empty:
        st.warning("File has no rows.")
        return None

    st.subheader("Preview")
    st.dataframe(df.head(20), use_container_width=True)

    columns = list(df.columns)
    col1, col2 = st.columns(2)
    with col1:
        column_to_fill = st.selectbox("Column to autofill", columns)
    with col2:
        limit_column = st.selectbox(
            "Limit column",
            columns,
            index=1 if len(columns) > 1 else 0,
            help="Autofill continues only while this column has data.",
        )

    if not st.button("Autofill Column", type="primary"):
        return None

    try:
        result = autofill_column(df, column_to_fill, limit_column)
    except ValueError as exc:
        st.error(str(exc))
        return None

    output_column = f"{column_to_fill}_autofilled"
    st.session_state.autofill_output_column = output_column
    st.session_state.autofill_result = result
    filled_count = _filled_count(df[column_to_fill], result[output_column])

    st.success(f"Created `{output_column}` and filled {filled_count} blank cells.")
    st.dataframe(result.head(50), use_container_width=True)

    if allow_download:
        _download_result(result, filename)

    return result


def _render_quick_guide():
    with st.expander("How this works"):
        st.markdown(
            """
1. Upload an Excel/CSV file or load a Google Sheet.
2. Choose the column you want to autofill.
3. Choose the limit column that controls where autofill should continue.
4. The app creates a new `<column>_autofilled` column.
5. Download the result or write the new column back to Google Sheets.

Example: if `Category` has a value in row 1 and blank cells below it, and `Product` still has data, the app fills those blanks with the row 1 category. When `Product` is blank, autofill stops.
            """.strip()
        )


def _load_uploaded_dataframe(uploaded) -> pd.DataFrame:
    suffix = Path(uploaded.name).suffix.lower()
    data = uploaded.getvalue()

    if suffix == ".csv":
        return pd.read_csv(io.BytesIO(data))
    if suffix in {".xls", ".xlsx"}:
        return pd.read_excel(io.BytesIO(data))
    raise ValueError("Unsupported file format. Upload CSV, XLS, or XLSX.")


def _download_result(df: pd.DataFrame, filename: str):
    suffix = Path(filename).suffix.lower()
    stem = Path(filename).stem or "data"

    if suffix in {".xls", ".xlsx"}:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        st.download_button(
            "Download Excel",
            data=buffer.getvalue(),
            file_name=f"autofilled_{stem}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return

    csv_data = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Download CSV",
        data=csv_data,
        file_name=f"autofilled_{stem}.csv",
        mime="text/csv",
    )


def _sheets_service():
    creds = get_credentials()
    if not creds:
        raise ValueError("Google authentication is required.")
    return build("sheets", "v4", credentials=creds)


def _load_sheet_dataframe(service, spreadsheet_id: str, worksheet: str) -> pd.DataFrame:
    response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=worksheet)
        .execute()
    )
    values = response.get("values", [])
    if not values:
        raise ValueError("Sheet is empty.")

    headers = values[0]
    rows = [row + [""] * (len(headers) - len(row)) for row in values[1:]]
    return pd.DataFrame(rows, columns=headers)


def _write_sheet_column(service, spreadsheet_id: str, worksheet: str, column_name: str, values) -> None:
    header_response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{worksheet}!1:1")
        .execute()
    )
    headers = header_response.get("values", [[]])[0]
    column_index = len(headers) + 1

    if column_name in headers:
        column_index = headers.index(column_name) + 1
    else:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{worksheet}!{_column_letter(column_index)}1",
            valueInputOption="RAW",
            body={"values": [[column_name]]},
        ).execute()

    body = {"values": [[_sheet_cell_value(value)] for value in values]}
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{worksheet}!{_column_letter(column_index)}2",
        valueInputOption="RAW",
        body=body,
    ).execute()


def _extract_spreadsheet_id(value: str) -> str:
    value = value.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]+", value):
        return value
    return ""


def _column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _sheet_cell_value(value):
    return "" if pd.isna(value) else value


def _has_value(value) -> bool:
    return pd.notna(value) and str(value).strip() != ""


def _filled_count(original: pd.Series, filled: pd.Series) -> int:
    original_empty = original.apply(lambda value: not _has_value(value))
    filled_has_value = filled.apply(_has_value)
    return int((original_empty & filled_has_value).sum())
