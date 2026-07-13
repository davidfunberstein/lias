"""Bilingual string registry — Hebrew / English."""
from __future__ import annotations

_STRINGS: dict[str, dict[str, str]] = {
    # --- startup ---
    "lang_prompt":          {"he": "שפה",            "en": "Language"},
    "lang_choose":          {"he": "[he/en, ברירת מחדל: he]: ", "en": "[he/en, default he]: "},

    # --- welcome ---
    "welcome_title":        {"he": "LEGAL-AI — ברוכים הבאים",   "en": "LEGAL-AI — Welcome"},
    "welcome_private":      {"he": "  1  לקוח פרטי — הורדת תיקים אישיים", "en": "  1  Private Client — download personal case files"},
    "welcome_lawyer":       {"he": "  2  עורך דין / טוען רבני — ניהול תיקי לקוחות", "en": "  2  Lawyer / Rabbinical Pleader — manage client cases"},
    "welcome_settings":     {"he": "  s  הגדרות",   "en": "  s  Settings"},
    "welcome_quit":         {"he": "  q  יציאה",    "en": "  q  Quit"},
    "welcome_prompt":       {"he": "בחר (1 / 2 / s / q): ", "en": "Enter choice (1 / 2 / s / q): "},

    # --- entity type (values always in Hebrew — filled into the portal) ---
    "entity_title":         {"he": ">>> כניסה בתור מה?",           "en": ">>> Login as:"},
    "entity_private":       {"he": "  1. גורם פרטי  [ברירת מחדל]", "en": "  1. גורם פרטי  [Default]"},
    "entity_lawyer":        {"he": '  2. עו"ד מייצג',              "en": '  2. עו"ד מייצג'},
    "entity_pleader":       {"he": '  3. טו"ר מייצג',              "en": '  3. טו"ר מייצג'},
    "entity_prompt":        {"he": ">>> [1/2/3, Enter = 1]: ",      "en": ">>> [1/2/3, Enter = 1]: "},
    "entity_set":           {"he": "כניסה כ-",                     "en": "Logging in as: "},

    # --- mode selected ---
    "mode_private":         {"he": "מצב לקוח פרטי נבחר.",      "en": "Private client mode selected."},
    "mode_lawyer":          {"he": "מצב עורך דין נבחר.",       "en": "Lawyer mode selected."},

    # --- main menu ---
    "menu_active_lawyer":   {"he": "[פעיל] עו\"ד",             "en": "[ACTIVE] Lawyer"},
    "menu_active_private":  {"he": "[פעיל] לקוח פרטי",        "en": "[ACTIVE] Private Client"},
    "menu_mode_updates":    {"he": "עדכון בלבד",               "en": "Updates Only"},
    "menu_mode_full":       {"he": "הורדה מחדש",               "en": "Full Re-download"},
    "menu_storage_local":   {"he": "מקומי",                    "en": "Local"},
    "menu_storage_both":    {"he": "מקומי + Drive",            "en": "Local + Drive"},
    "menu_storage_cloud":   {"he": "Drive בלבד",               "en": "Drive Only"},
    "menu_1":               {"he": "  1  BDR — בתי הדין הרבניים",         "en": "  1  BDR — Rabbinical Courts"},
    "menu_2":               {"he": "  2  NET — נט המשפט",                   "en": "  2  NET — Net HaMishpat"},
    "menu_settings":        {"he": "  s  הגדרות",              "en": "  s  Settings"},
    "menu_quit_he":         {"he": "  q  יציאה",               "en": "  q  Exit"},
    "menu_prompt":          {"he": "בחר (1/2/s/q): ",          "en": "Enter choice (1/2/s/q): "},

    # BDR sub-menu
    "bdr_sub_title":        {"he": "BDR — בתי הדין הרבניים",  "en": "BDR — Rabbinical Courts"},
    "bdr_sub_1":            {"he": "  1  תיק בודד",           "en": "  1  Single case"},
    "bdr_sub_2":            {"he": "  2  כל התיקים (אצווה)",  "en": "  2  All cases (batch)"},
    "bdr_sub_back":         {"he": "  b  חזרה",               "en": "  b  Back"},
    "bdr_sub_prompt":       {"he": "בחר (1/2/b): ",           "en": "Enter choice (1/2/b): "},

    # NET sub-menu
    "net_sub_title":        {"he": "NET — נט המשפט",          "en": "NET — Net HaMishpat"},
    "net_sub_1":            {"he": "  1  תיק בודד",           "en": "  1  Single case"},
    "net_sub_2":            {"he": "  2  עדכון כל התיקים הקיימים", "en": "  2  Update all existing cases"},
    "net_sub_3":            {"he": "  3  חיפוש והורדה לפי טווח תאריכים", "en": "  3  Search & download by date range"},
    "net_sub_back":         {"he": "  b  חזרה",               "en": "  b  Back"},
    "net_sub_prompt":       {"he": "בחר (1/2/3/b): ",         "en": "Enter choice (1/2/3/b): "},

    # --- settings menu ---
    "settings_title":       {"he": "הגדרות",                              "en": "SETTINGS"},
    "settings_sec_conn":    {"he": "  ── חיבור ואוטומציה ──",             "en": "  ── Connection & Automation ──"},
    "settings_1":           {"he": "  1  פרטי כניסה (ת.ז. + סיסמה)",     "en": "  1  Credentials (ID + password)"},
    "settings_2":           {"he": "  2  שיטת כניסה (סיסמה / Passkey)",  "en": "  2  Login method (Password / Passkey)"},
    "settings_3":           {"he": "  3  מייל OTP — קריאה אוטומטית",     "en": "  3  Email OTP — auto-read"},
    "settings_sec_dl":      {"he": "  ── הורדות ──",                      "en": "  ── Downloads ──"},
    "settings_4":           {"he": "  4  מצב הורדה (עדכון בלבד / מחדש)", "en": "  4  Download mode (Updates Only / Full Re-download)"},
    "settings_5":           {"he": "  5  סינון לפי תאריך",               "en": "  5  Date range filter"},
    "settings_6":           {"he": "  6  תיקים קשורים NET (כן/לא)",       "en": "  6  NET related cases (yes/no)"},
    "settings_sec_storage": {"he": "  ── אחסון ──",                       "en": "  ── Storage ──"},
    "settings_7":           {"he": "  7  Google Drive",                   "en": "  7  Google Drive sync"},
    "settings_sec_user":    {"he": "  ── משתמש ──",                       "en": "  ── User ──"},
    "settings_8":           {"he": "  8  מצב משתמש (עו\"ד / פרטי)",       "en": "  8  User mode (Lawyer / Private)"},
    "settings_9":           {"he": "  9  שם עורך דין (לזיהוי לקוח)",     "en": "  9  Lawyer name (for client identification)"},
    "settings_sec_ocr":     {"he": "  ── OCR ──",                         "en": "  ── OCR ──"},
    "settings_10":          {"he": " 10  OCR — המרת PDF סרוק לטקסט (Gemini)", "en": " 10  OCR — scanned PDF to text (Gemini)"},
    "settings_back":        {"he": "  b  חזרה",                           "en": "  b  Back"},
    "settings_prompt":      {"he": "בחר (1/2/3/4/5/6/b): ",              "en": "Enter choice (1/2/3/4/5/6/b): "},
    "settings_prompt2":     {"he": "בחר (1–9 / b): ",                    "en": "Enter choice (1–9 / b): "},
    "lawyer_name_current":  {"he": "שם עורך דין נוכחי: ",            "en": "Current lawyer name: "},
    "lawyer_name_not_set":  {"he": "(לא הוגדר)",                     "en": "(not set)"},
    "lawyer_name_prompt":   {"he": "הכנס שם עורך דין (Enter לביטול): ", "en": "Enter lawyer name (Enter to cancel): "},
    "lawyer_name_set":      {"he": "שם עורך דין נקבע: ",             "en": "Lawyer name set to: "},
    "lawyer_name_cleared":  {"he": "שם עורך דין נמחק.",              "en": "Lawyer name cleared."},
    "client_inferred":      {"he": "לקוח זוהה: {client}",            "en": "Client identified: {client}"},
    "client_not_inferred":  {"he": "לא ניתן לזהות לקוח — ממשיך ללא ארגון לפי לקוח.", "en": "Could not identify client — continuing without client folder."},
    "lawyer_mismatch":      {"he": "⚠️  חוסר תאימות בשם עורך דין: {names}", "en": "⚠️  Lawyer name mismatch in CSV data: {names}"},

    # --- connection prompts ---
    "conn_connected":       {"he": ">>> סטטוס: מחובר ל-{portal}.", "en": ">>> STATUS: Connected to {portal}."},
    "conn_navigate":        {"he": ">>> 1. נווט בדפדפן לתיק הרצוי.", "en": ">>> 1. Navigate your browser to the desired case."},
    "conn_ensure_grid":     {"he": ">>> 2. ודא שרשת המסמכים גלויה.", "en": ">>> 2. Ensure the document grid is visible."},
    "conn_press_enter":     {"he": ">>> 3. לחץ ENTER להתחלת הסנכרון, או 'b' לחזרה לתפריט הראשי.", "en": ">>> 3. Press ENTER to begin syncing, or type 'b' to go back."},
    "conn_prompt":          {"he": ">>> [Enter / b]: ",         "en": ">>> [Enter / b]: "},

    # --- auth messages ---
    "auth_already_net":     {"he": "כבר מחובר ל-NET — מדלג על הזדהות.", "en": "Already logged in to NET — skipping authentication."},
    "auth_already_bdr":     {"he": "כבר מחובר ל-BDR — מדלג על הזדהות.", "en": "Already logged in to BDR — skipping authentication."},
    "auth_starting":        {"he": "מתחיל התחברות אוטומטית ל-{portal}...", "en": "Starting auto-login for {portal}..."},
    "auth_no_creds":        {"he": "אין פרטי כניסה שמורים — אנא התחבר ידנית.", "en": "No saved credentials — please log in manually."},
    "auth_tip":             {"he": "טיפ: הפעל 'c' מהתפריט הראשי לשמירת פרטים.", "en": "Tip: run 'c' from the main menu to save credentials."},
    "auth_waiting_redirect":{"he": "ממתין לסיום ההפניה...",   "en": "Waiting for login redirect to complete..."},
    "auth_complete":        {"he": "התחברות הושלמה — הופנה ל: {url}", "en": "Login complete — redirected to: {url}"},
    "auth_timeout":         {"he": "פג הזמן בהמתנה להפניה — ממשיך.", "en": "Timed out waiting for redirect — continuing."},

    # --- bdr auth ---
    "bdr_login_detected":   {"he": "דף כניסה ל-BDR זוהה.",    "en": "BDR login page detected."},
    "bdr_entity_options":   {"he": "סוגי גורמים זמינים:",     "en": "Available entity types:"},
    "bdr_clicking_enter":   {"he": "לוחץ 'כניסה למערכת'...",  "en": "Clicking 'כניסה למערכת'..."},
    "bdr_clicked_enter":    {"he": "לחץ 'כניסה למערכת' — מועבר ל-login.gov.il...", "en": "Clicked 'כניסה למערכת' — redirecting to login.gov.il..."},
    "bdr_not_found":        {"he": "כפתור 'כניסה למערכת' לא נמצא — אנא לחץ ידנית.", "en": "Could not find 'כניסה למערכת' button — please click manually."},
    "bdr_lawyer_auto":      {"he": "מצב עו\"ד — בוחר אוטומטית: {entity}", "en": "Lawyer mode — auto-selecting entity type: {entity}"},

    # --- info/status ---
    "info_launching":       {"he": "מפעיל דפדפן...",          "en": "Launching persistent browser instance..."},
    "info_reusing":         {"he": "משתמש שוב בכרטיסייה הפעילה.", "en": "Re-using active browser tab."},
    "info_returning":       {"he": "חוזר לתפריט הראשי.",      "en": "Returning to main menu."},
    "info_interrupted":     {"he": "הופסק — חוזר לתפריט הראשי (הדפדפן נשאר פתוח).", "en": "Interrupted — returning to main menu (browser stays open)."},
    "info_terminated":      {"he": "מסיים סשן. להתראות!",     "en": "Terminating session. Goodbye!"},
    "info_goodbye":         {"he": "להתראות!",                "en": "Goodbye!"},
    "info_conn_lost":       {"he": "חיבור לדפדפן אבד — יופעל מחדש בבחירה הבאה.", "en": "Browser connection lost — will relaunch on next selection."},

    # --- option 6 sub-menu ---
    "opt6_title":           {"he": "אפשרות 6 — NET:",               "en": "Option 6 — NET:"},
    "opt6_update_all":      {"he": "  1  עדכון כל התיקים הקיימים (מתיקיות מקומיות)", "en": "  1  Update all existing NET cases (from local folders)"},
    "opt6_new_case":        {"he": "  2  הורד תיק NET חדש", "en": "  2  Download a new NET case"},
    "opt6_bulk_search":     {"he": "  3  הורד את כל תיקי NET — חיפוש לפי טווח שנים", "en": "  3  Download all NET cases — search by date range (years back)"},
    "opt6_prompt":          {"he": "בחר (1 / 2 / 3 / b): ",         "en": "Enter choice (1 / 2 / 3 / b): "},

    # --- smart path / party selection ---
    "smart_path_title":     {"he": "נמצאו מספר צדדים. בחר תיקיית יעד:", "en": "Multiple parties found. Choose the target folder:"},
    "smart_path_prompt":    {"he": "בחר מספר צד (1-{n}): ",              "en": "Select party index (1-{n}): "},
    "smart_path_invalid":   {"he": "בחירה לא תקינה — נסה שוב.",          "en": "Invalid choice — try again."},
    "smart_path_created":   {"he": "נוצרה תיקיית צד: ",                  "en": "Created party directory: "},
    "smart_path_matched":   {"he": "[Smart Path] הותאמה תיקייה קיימת: ", "en": "[Smart Path] Matched existing party dir: "},

    # --- passkey ---
    "passkey_detected":     {"he": "בחר שיטת כניסה:", "en": "Choose login method:"},
    "passkey_option_p":     {"he": "  p     → כניסה מהירה (Passkey) — אשר במכשיר שלך", "en": "  p     → Quick login (Passkey) — approve on your device"},
    "passkey_option_enter": {"he": "  Enter → כניסה עם סיסמה + קוד OTP",              "en": "  Enter → Sign in with password + OTP code"},
    "passkey_prompt":       {"he": "בחירה (p / Enter):",                                "en": "Choice (p / Enter):"},
    "passkey_waiting":      {"he": "לחץ 'כניסה מהירה' — ממתין לאישור במכשיר...",     "en": "Clicked Quick Login — waiting for device approval..."},
    "passkey_success":      {"he": "כניסה מהירה הצליחה.",                              "en": "Quick login succeeded."},
    "passkey_timeout":      {"he": "כניסה מהירה לא הושלמה תוך {n} שניות.",           "en": "Quick login timed out after {n} seconds."},
    "passkey_fallback":     {"he": "כניסה מהירה נכשלה — ממשיך עם סיסמה + OTP.",      "en": "Quick login failed — falling back to password + OTP."},

    # --- batch ---
    "batch_lawyer_ready":   {"he": "מצב עו\"ד — אצווה BDR מוכן.",  "en": "Lawyer mode — BDR batch ready."},
    "batch_download_all":   {"he": ">>> להוריד את כל התיקים? (y / b לחזרה): ", "en": ">>> Download ALL cases now? (y / b to go back): "},
    "batch_bdr_ready":      {"he": ">>> סטטוס: הדפדפן נמצא בפורטל BDR.", "en": ">>> STATUS: Browser is on BDR portal."},
    "batch_wait_ready":     {"he": ">>> כשתראה 'תיקים שלי', לחץ ENTER להתחלת האצווה.", "en": ">>> Once you see 'תיקים שלי', press ENTER to start batch."},
    "batch_back":           {"he": ">>> הקלד 'b' לחזרה לתפריט הראשי.", "en": ">>> Type 'b' to go back to main menu."},
}


def t(key: str, **kwargs) -> str:
    """Return the string for the current UI language. Falls back to Hebrew."""
    try:
        from core.download import SESSION_SETTINGS
        lang = SESSION_SETTINGS.get("lang", "he")
    except Exception:
        lang = "he"
    entry = _STRINGS.get(key, {})
    text = entry.get(lang) or entry.get("he") or key
    return text.format(**kwargs) if kwargs else text
