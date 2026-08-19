import { useCallback, useEffect, useState } from "react";
import { api, type LogRow } from "./api";
import { clock, usePoll } from "./usePoll";

const LABELS: Record<string, string> = {
  points_per_shekel: "נקודות לכל שקל",
  first_ride_gift: "מתנת נסיעה ראשונה",
  redeem_points: "עלות נסיעת חינם בנקודות",
  referral_points: "נקודות לכל נסיעה של מספר משויך",
  referral_confirm_hours: "שעות לאישור שיוך",
  referral_credit_days: "ימי זיכוי לאחר אישור",
  tender_window_seconds: "חלון הצעות (שניות)",
  location_fresh_hours: "תוקף עדכון מיקום (שעות)",
  rating_delay_minutes: "השהיית שיחת דירוג (דקות)",
  commission_rate: "שיעור עמלה",
  public_base_url: "כתובת ציבורית למרכזייה",
  auto_tender: "פתיחת מכרז אוטומטית (1/0)",
};

export function Settings() {
  const [values, setValues] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");

  useEffect(() => {
    api
      .settings()
      .then(setValues)
      .catch((err: Error) => setNote(err.message));
  }, []);

  return (
    <>
      <h1>הגדרות</h1>
      {note && <div className="muted">{note}</div>}
      <div className="grid">
        {Object.entries(values).map(([key, value]) => (
          <label key={key}>
            {LABELS[key] ?? key}
            <input
              value={value}
              onChange={(e) => setValues({ ...values, [key]: e.target.value })}
            />
          </label>
        ))}
      </div>
      <button
        className="action"
        onClick={() =>
          api
            .saveSettings(values)
            .then((saved) => {
              setValues(saved);
              setNote("נשמר");
            })
            .catch((err: Error) => setNote(err.message))
        }
      >
        שמור הגדרות
      </button>
      <Logs />
    </>
  );
}

/** Every points movement and driver change is here; the club is money, so the
 *  office needs to be able to answer "who did this". */
function Logs() {
  const [action, setAction] = useState("");
  const load = useCallback(() => api.logs(action || undefined), [action]);
  const { data, error } = usePoll<LogRow[]>(load, 20);

  return (
    <>
      <h2>לוג פעולות</h2>
      {error && <div className="error">{error}</div>}
      <div className="row">
        <input
          placeholder="סינון לפי סוג פעולה"
          value={action}
          onChange={(e) => setAction(e.target.value)}
        />
      </div>
      <table>
        <thead>
          <tr>
            <th>מתי</th>
            <th>מי</th>
            <th>פעולה</th>
            <th>ישות</th>
            <th>פרטים</th>
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((row) => (
            <tr key={row.id}>
              <td data-label="מתי">{clock(row.created_at)}</td>
              <td data-label="מי">{row.actor}</td>
              <td data-label="פעולה">{row.action}</td>
              <td data-label="ישות">
                {row.entity ?? "—"} {row.entity_id ?? ""}
              </td>
              <td data-label="פרטים">{row.detail ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
