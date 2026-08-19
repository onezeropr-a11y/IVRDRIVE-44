import { useCallback, useState } from "react";
import {
  api,
  type ClubDetail,
  type ClubMember,
  type RatingRequest,
  type Referral,
} from "./api";
import { clock, usePoll } from "./usePoll";

const REFERRAL_STATUS: Record<string, string> = {
  pending: "ממתין לאישור",
  confirmed: "אושר",
  expired: "פג",
};

const RATING_STATUS: Record<string, string> = {
  scheduled: "מתוזמן",
  calling: "בחיוג",
  done: "דורג",
  failed: "נכשל",
  skipped: "דולג",
};

function Member({ phone, onClose }: { phone: string; onClose: () => void }) {
  const load = useCallback(() => api.clubMember(phone), [phone]);
  const { data, error, refresh } = usePoll<ClubDetail>(load, 20);
  const [delta, setDelta] = useState("");
  const [note, setNote] = useState("");

  if (!data) return <div className="muted">{error || "טוען…"}</div>;

  return (
    <div className="panel">
      <div className="row">
        <h2>
          {data.name ?? data.phone} · {data.balance} נקודות
        </h2>
        <button onClick={onClose}>סגור</button>
      </div>
      {data.can_redeem && <div className="muted">אפשר לממש נסיעת חינם.</div>}

      <div className="row">
        <input
          placeholder="שינוי נקודות (+/-)"
          value={delta}
          onChange={(e) => setDelta(e.target.value)}
        />
        <input placeholder="סיבה" value={note} onChange={(e) => setNote(e.target.value)} />
        <button
          className="action"
          onClick={() =>
            api.adjustPoints(data.phone, Number(delta), note).then(() => {
              setDelta("");
              setNote("");
              refresh();
            })
          }
        >
          עדכן ניקוד
        </button>
      </div>

      <h3>העדפות</h3>
      <div className="grid">
        <label>
          שם
          <input
            defaultValue={data.name ?? ""}
            onBlur={(e) => api.savePreferences(data.phone, { name: e.target.value }).then(refresh)}
          />
        </label>
        <label>
          כתובת איסוף קבועה
          <input
            defaultValue={data.preferences.default_pickup ?? ""}
            onBlur={(e) =>
              api.savePreferences(data.phone, { default_pickup: e.target.value }).then(refresh)
            }
          />
        </label>
        <label>
          נהג מועדף
          <input
            defaultValue={data.preferences.preferred_driver_phone ?? ""}
            onBlur={(e) =>
              api
                .savePreferences(data.phone, { preferred_driver_phone: e.target.value })
                .then(refresh)
            }
          />
        </label>
        <label>
          נהג חסום
          <input
            defaultValue={data.preferences.blocked_driver_phone ?? ""}
            onBlur={(e) =>
              api
                .savePreferences(data.phone, { blocked_driver_phone: e.target.value })
                .then(refresh)
            }
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={data.preferences.no_marketing}
            onChange={(e) =>
              api.savePreferences(data.phone, { no_marketing: e.target.checked }).then(refresh)
            }
          />
          לא לשלוח פרסומות
        </label>
      </div>

      <h3>היסטוריית ניקוד</h3>
      <table>
        <thead>
          <tr>
            <th>מתי</th>
            <th>שינוי</th>
            <th>סיבה</th>
            <th>הזמנה</th>
            <th>מי ביצע</th>
          </tr>
        </thead>
        <tbody>
          {data.history.map((row) => (
            <tr key={row.id}>
              <td data-label="מתי">{clock(row.created_at)}</td>
              <td data-label="שינוי">{row.delta > 0 ? `+${row.delta}` : row.delta}</td>
              <td data-label="סיבה">{row.note ?? row.reason}</td>
              <td data-label="הזמנה">{row.order_id ?? "—"}</td>
              <td data-label="מי ביצע">{row.actor}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>שתפו וסעו</h3>
      <table>
        <thead>
          <tr>
            <th>מספר משויך</th>
            <th>סטטוס</th>
            <th>זיכוי עד</th>
            <th>נסיעות שזוכו</th>
          </tr>
        </thead>
        <tbody>
          {data.referrals.map((row) => (
            <tr key={row.invited_phone}>
              <td data-label="מספר משויך">{row.invited_phone}</td>
              <td data-label="סטטוס">{REFERRAL_STATUS[row.status] ?? row.status}</td>
              <td data-label="זיכוי עד">{row.credit_until ? clock(row.credit_until) : "—"}</td>
              <td data-label="נסיעות שזוכו">{row.rewarded_orders}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Club() {
  const load = useCallback(() => api.clubMembers(), []);
  const { data, error } = usePoll<ClubMember[]>(load, 30);
  const [open, setOpen] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  return (
    <>
      <h1>מועדון נוסעים</h1>
      {error && <div className="error">{error}</div>}
      <div className="row">
        <input
          placeholder="חיפוש לפי טלפון"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button className="action" onClick={() => setOpen(search)}>
          פתח כרטיס
        </button>
      </div>
      {open && <Member phone={open} onClose={() => setOpen(null)} />}
      <table>
        <thead>
          <tr>
            <th>טלפון</th>
            <th>שם</th>
            <th>יתרה</th>
            <th>פעילות אחרונה</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(data ?? [])
            .filter((m) => m.phone.includes(search))
            .map((member) => (
              <tr key={member.phone}>
                <td data-label="טלפון">{member.phone}</td>
                <td data-label="שם">{member.name ?? "—"}</td>
                <td data-label="יתרה">{member.balance}</td>
                <td data-label="פעילות אחרונה">
                  {member.last_at ? clock(member.last_at) : "—"}
                </td>
                <td>
                  <button onClick={() => setOpen(member.phone)}>כרטיס</button>
                </td>
              </tr>
            ))}
        </tbody>
      </table>
      <Referrals />
      <Ratings />
    </>
  );
}

function Referrals() {
  const load = useCallback(() => api.referrals(), []);
  const { data, error, refresh } = usePoll<Referral[]>(load, 30);
  const [form, setForm] = useState({ referrer: "", invited: "", flash: true });
  const [note, setNote] = useState("");

  return (
    <>
      <h2>שיוכי שתפו וסעו</h2>
      {(error || note) && <div className={error ? "error" : "muted"}>{error || note}</div>}
      <div className="row">
        <input
          placeholder="טלפון המפנה"
          value={form.referrer}
          onChange={(e) => setForm({ ...form, referrer: e.target.value })}
        />
        <input
          placeholder="טלפון המשויך"
          value={form.invited}
          onChange={(e) => setForm({ ...form, invited: e.target.value })}
        />
        <label className="check">
          <input
            type="checkbox"
            checked={form.flash}
            onChange={(e) => setForm({ ...form, flash: e.target.checked })}
          />
          לצנתק למספר המשויך
        </label>
        <button
          className="action"
          onClick={() =>
            api
              .createReferral(form.referrer, form.invited, form.flash)
              .then(() => {
                setForm({ referrer: "", invited: "", flash: true });
                setNote("השיוך נרשם וממתין לחיוג מהמספר המשויך");
                refresh();
              })
              .catch((err: Error) => setNote(err.message))
          }
        >
          שייך מספר
        </button>
      </div>
      <table>
        <thead>
          <tr>
            <th>מפנה</th>
            <th>משויך</th>
            <th>סטטוס</th>
            <th>תוקף אישור</th>
            <th>זיכוי עד</th>
            <th>נסיעות שזוכו</th>
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((row) => (
            <tr key={row.id}>
              <td data-label="מפנה">{row.referrer_phone}</td>
              <td data-label="משויך">{row.invited_phone}</td>
              <td data-label="סטטוס">{REFERRAL_STATUS[row.status] ?? row.status}</td>
              <td data-label="תוקף אישור">{clock(row.expires_at)}</td>
              <td data-label="זיכוי עד">{row.credit_until ? clock(row.credit_until) : "—"}</td>
              <td data-label="נסיעות שזוכו">{row.rewarded_orders}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function Ratings() {
  const load = useCallback(() => api.ratings(), []);
  const { data, error, refresh } = usePoll<RatingRequest[]>(load, 20);

  return (
    <>
      <h2>שיחות דירוג</h2>
      {error && <div className="error">{error}</div>}
      <table>
        <thead>
          <tr>
            <th>הזמנה</th>
            <th>נוסע</th>
            <th>מועד חיוג</th>
            <th>סטטוס</th>
            <th>ציון</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((row) => (
            <tr key={row.id}>
              <td data-label="הזמנה">{row.order_id}</td>
              <td data-label="נוסע">{row.phone}</td>
              <td data-label="מועד חיוג">{clock(row.due_at)}</td>
              <td data-label="סטטוס">{RATING_STATUS[row.status] ?? row.status}</td>
              <td data-label="ציון">{row.score ?? "—"}</td>
              <td>
                {row.status !== "done" && (
                  <button onClick={() => api.callRating(row.id).then(refresh)}>חייג עכשיו</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
