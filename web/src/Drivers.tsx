import { useCallback, useState } from "react";
import { api, type Area, type Driver } from "./api";
import { clock, usePoll } from "./usePoll";

const STATUS_LABEL: Record<string, string> = {
  pending: "ממתין לאישור",
  active: "פעיל",
  paused: "מושהה",
  removed: "הוסר",
};

const empty: Partial<Driver> = { phone: "", status: "pending", smartphone: true };

function DriverForm({ driver, onDone }: { driver: Partial<Driver>; onDone: () => void }) {
  const [form, setForm] = useState<Partial<Driver>>(driver);
  const [error, setError] = useState("");

  const field = (key: keyof Driver, value: string | number | boolean | string[] | null) =>
    setForm((current) => ({ ...current, [key]: value }));

  return (
    <div className="panel">
      <h2>{form.id ? `עריכת נהג ${form.phone}` : "נהג חדש"}</h2>
      {error && <div className="error">{error}</div>}
      <div className="grid">
        <label>
          טלפון
          <input
            value={form.phone ?? ""}
            disabled={Boolean(form.id)}
            onChange={(e) => field("phone", e.target.value)}
          />
        </label>
        <label>
          שם
          <input value={form.name ?? ""} onChange={(e) => field("name", e.target.value)} />
        </label>
        <label>
          דגם רכב
          <input
            value={form.car_model ?? ""}
            onChange={(e) => field("car_model", e.target.value)}
          />
        </label>
        <label>
          שנת רכב
          <input
            type="number"
            value={form.car_year ?? ""}
            onChange={(e) => field("car_year", Number(e.target.value))}
          />
        </label>
        <label>
          מספר מושבים
          <input
            type="number"
            value={form.seats ?? ""}
            onChange={(e) => field("seats", Number(e.target.value))}
          />
        </label>
        <label>
          שנת לידה
          <input
            type="number"
            value={form.birth_year ?? ""}
            onChange={(e) => field("birth_year", Number(e.target.value))}
          />
        </label>
        <label>
          אזורים מועדפים (מופרדים בפסיק)
          <input
            value={(form.areas ?? []).join(", ")}
            onChange={(e) =>
              field(
                "areas",
                e.target.value
                  .split(",")
                  .map((a) => a.trim())
                  .filter(Boolean),
              )
            }
          />
        </label>
        <label>
          שעות שקט (משעה)
          <input
            type="number"
            value={form.quiet_from ?? ""}
            onChange={(e) => field("quiet_from", Number(e.target.value))}
          />
        </label>
        <label>
          שעות שקט (עד שעה)
          <input
            type="number"
            value={form.quiet_to ?? ""}
            onChange={(e) => field("quiet_to", Number(e.target.value))}
          />
        </label>
        <label>
          סטטוס
          <select value={form.status ?? "pending"} onChange={(e) => field("status", e.target.value)}>
            {Object.entries(STATUS_LABEL).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={Boolean(form.smartphone)}
            onChange={(e) => field("smartphone", e.target.checked)}
          />
          סמארטפון
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={Boolean(form.voice_offers)}
            onChange={(e) => field("voice_offers", e.target.checked)}
          />
          הודעה קולית בתשלום (במקום צינתוק)
        </label>
      </div>
      <div className="row">
        <button
          className="action"
          onClick={() =>
            api
              .saveDriver(form)
              .then(onDone)
              .catch((err: Error) => setError(err.message))
          }
        >
          שמור
        </button>
        <button onClick={onDone}>ביטול</button>
      </div>
    </div>
  );
}

export function Drivers() {
  const load = useCallback(() => api.drivers(), []);
  const { data, error, refresh } = usePoll<Driver[]>(load, 20);
  const [editing, setEditing] = useState<Partial<Driver> | null>(null);
  const [note, setNote] = useState("");

  const act = (promise: Promise<unknown>, message: string) =>
    promise
      .then(() => {
        setNote(message);
        refresh();
      })
      .catch((err: Error) => setNote(err.message));

  return (
    <>
      <h1>נהגים</h1>
      {(error || note) && <div className={error ? "error" : "muted"}>{error || note}</div>}
      <div className="row">
        <button className="action" onClick={() => setEditing(empty)}>
          נהג חדש
        </button>
      </div>
      {editing && (
        <DriverForm
          driver={editing}
          onDone={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
      <table>
        <thead>
          <tr>
            <th>טלפון</th>
            <th>שם</th>
            <th>רכב</th>
            <th>אזורים</th>
            <th>דירוג</th>
            <th>נסיעות</th>
            <th>מוניטין</th>
            <th>מיקום אחרון</th>
            <th>סטטוס</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((driver) => (
            <tr key={driver.id}>
              <td data-label="טלפון">{driver.phone}</td>
              <td data-label="שם">{driver.name ?? "—"}</td>
              <td data-label="רכב">
                {driver.car_model ?? "—"} {driver.car_year ?? ""}
              </td>
              <td data-label="אזורים">{driver.areas.join(", ") || "—"}</td>
              <td data-label="דירוג">
                {driver.rating ? `${driver.rating.toFixed(1)} (${driver.rating_count})` : "—"}
              </td>
              <td data-label="נסיעות">{driver.rides_done}</td>
              <td data-label="מוניטין">{driver.tier_label}</td>
              <td data-label="מיקום אחרון">
                {driver.last_area ?? "—"}
                {driver.last_area_at ? ` · ${clock(driver.last_area_at)}` : ""}
              </td>
              <td data-label="סטטוס">{STATUS_LABEL[driver.status] ?? driver.status}</td>
              <td>
                <button onClick={() => setEditing(driver)}>עריכה</button>
                <button onClick={() => act(api.driverFlash(driver.id), "צינתוק נשלח")}>
                  צינתוק
                </button>
                <button onClick={() => act(api.removeDriver(driver.id), "הנהג הוסר")}>
                  הסרה
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Areas />
    </>
  );
}

/** Each area rings on its own number, so the driver knows where the ride is
 *  before answering — that is the whole point of a flash call. */
function Areas() {
  const load = useCallback(() => api.areas(), []);
  const { data, error, refresh } = usePoll<Area[]>(load, 60);
  const [form, setForm] = useState({ name: "", callback_number: "", flash_cid: "" });

  return (
    <>
      <h2>אזורי צינתוק</h2>
      {error && <div className="error">{error}</div>}
      <table>
        <thead>
          <tr>
            <th>אזור</th>
            <th>מספר לחיוג חוזר</th>
            <th>מזהה מתקשר</th>
            <th>פעיל</th>
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((area) => (
            <tr key={area.id}>
              <td data-label="אזור">{area.name}</td>
              <td data-label="מספר לחיוג חוזר">{area.callback_number ?? "—"}</td>
              <td data-label="מזהה מתקשר">{area.flash_cid ?? "—"}</td>
              <td data-label="פעיל">{area.active ? "כן" : "לא"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <input
          placeholder="שם אזור"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
        <input
          placeholder="מספר לחיוג חוזר"
          value={form.callback_number}
          onChange={(e) => setForm({ ...form, callback_number: e.target.value })}
        />
        <input
          placeholder="מזהה מתקשר לצינתוק"
          value={form.flash_cid}
          onChange={(e) => setForm({ ...form, flash_cid: e.target.value })}
        />
        <button
          className="action"
          onClick={() =>
            api.saveArea(form).then(() => {
              setForm({ name: "", callback_number: "", flash_cid: "" });
              refresh();
            })
          }
        >
          שמור אזור
        </button>
      </div>
    </>
  );
}
