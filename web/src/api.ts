/**
 * The console talks to the backend service across origins, so the base URL is
 * a build-time variable and every call carries the operator token.
 */
const BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");

export const tokenStore = {
  get: () => localStorage.getItem("drivers.token") ?? "",
  set: (value: string) => localStorage.setItem("drivers.token", value),
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      "x-admin-token": tokenStore.get(),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

export type OrderStatus = "new" | "assigned" | "on_route" | "done" | "cancelled";

export interface Order {
  id: number;
  created_at: string;
  phone: string;
  origin: string;
  destination: string;
  passengers: number;
  pickup_time: string | null;
  price: number | null;
  notes: string | null;
  status: OrderStatus;
  driver_name: string | null;
  driver_phone: string | null;
}

export interface Call {
  id: number;
  call_id: string;
  phone: string | null;
  started_at: string;
  summary: string | null;
  cost_usd: number;
}

export interface CallDetail extends Omit<Call, "cost_usd"> {
  transcript: string | null;
  stats: {
    turns?: number;
    interruptions?: number;
    reply_latency_ms?: number[];
    tool_calls?: { name: string; result: unknown }[];
    usage?: {
      cost_usd?: number;
      input_tokens?: Record<string, number>;
      output_tokens?: Record<string, number>;
    };
  };
}

export interface Summary {
  orders_24h: number;
  calls_24h: number;
  cost_usd_24h: number;
  by_status: Record<OrderStatus, number>;
}

export interface Price {
  id: number;
  origin: string;
  destination: string;
  price: number;
}

export interface Customer {
  id: number;
  phone: string;
  name: string | null;
  default_pickup: string | null;
  notes: string | null;
}

export interface Driver {
  id: number;
  phone: string;
  name: string | null;
  home_area: string | null;
  areas: string[];
  car_model: string | null;
  car_year: number | null;
  seats: number | null;
  birth_year: number | null;
  smartphone: boolean;
  voice_offers: boolean;
  quiet_from: number | null;
  quiet_to: number | null;
  status: string;
  rating: number;
  rating_count: number;
  rides_done: number;
  score: number;
  tier: string;
  tier_label: string;
  last_area: string | null;
  last_area_at: string | null;
  notes: string | null;
}

export interface BoardArea {
  area: string;
  drivers: {
    id: number;
    name: string | null;
    phone: string;
    tier: string;
    minutes_ago: number;
    score: number;
  }[];
}

export interface Area {
  id: number;
  name: string;
  callback_number: string | null;
  flash_cid: string | null;
  active: boolean;
}

/** What the dispatcher may narrow a blast to: a new car, an older driver,
 *  someone who definitely has no smartphone, and so on. */
export interface TenderFilters {
  min_car_year?: number;
  min_seats?: number;
  min_age?: number;
  min_rating?: number;
  smartphone?: boolean;
  voice_offers?: boolean;
  tiers?: string[];
}

export interface Tender {
  id: number;
  order_id: number;
  area: string | null;
  status: string;
  opened_at: string;
  closes_at: string;
  notified: number;
  bids: number;
  awarded_driver_id: number | null;
  filters: TenderFilters;
}

export interface ClubMember {
  phone: string;
  name: string | null;
  balance: number;
  last_at: string | null;
}

export interface ClubDetail {
  phone: string;
  name: string | null;
  balance: number;
  can_redeem: boolean;
  preferences: {
    preferred_driver_phone: string | null;
    blocked_driver_phone: string | null;
    no_marketing: boolean;
    default_pickup: string | null;
  };
  history: {
    id: number;
    delta: number;
    reason: string;
    order_id: number | null;
    actor: string;
    note: string | null;
    created_at: string;
  }[];
  referrals: {
    invited_phone: string;
    status: string;
    confirmed_at: string | null;
    credit_until: string | null;
    rewarded_orders: number;
  }[];
}

export interface Referral {
  id: number;
  referrer_phone: string;
  invited_phone: string;
  status: string;
  created_at: string;
  expires_at: string;
  confirmed_at: string | null;
  credit_until: string | null;
  rewarded_orders: number;
}

export interface RatingRequest {
  id: number;
  order_id: number;
  driver_id: number | null;
  phone: string;
  due_at: string;
  status: string;
  score: number | null;
  attempts: number;
}

export interface Books {
  days: number;
  rides_done: number;
  fares: number;
  commission_income: number;
  expenses: number;
  profit: number;
  expenses_by_category: Record<string, number>;
  point_rides: number;
  points_outstanding: number;
  points_liability_rides: number;
}

export interface DriverRides {
  driver_id: number;
  name: string | null;
  phone: string | null;
  rides: number;
  fares: number;
  commission: number;
}

export interface Statement {
  driver: { id: number; name: string | null; phone: string };
  days: number;
  rides: {
    order_id: number;
    date: string;
    origin: string;
    destination: string;
    price: number;
    paid_with_points: boolean;
    commission: number;
  }[];
  total_fares: number;
  total_commission: number;
  text?: string;
  sent?: boolean;
}

export interface Expense {
  id: number;
  spent_on: string;
  category: string;
  amount: number;
  note: string | null;
}

export interface LogRow {
  id: number;
  created_at: string;
  actor: string;
  action: string;
  entity: string | null;
  entity_id: string | null;
  detail: string | null;
}

/** Attribution, not authentication: the token says the console may act, this
 *  says which person at the desk did. */
export const actorStore = {
  get: () => localStorage.getItem("drivers.actor") ?? "",
  set: (value: string) => localStorage.setItem("drivers.actor", value),
};

const write = <T,>(path: string, body: unknown, method = "POST") =>
  request<T>(path, {
    method,
    headers: { "x-actor": actorStore.get() || "console" },
    body: JSON.stringify(body ?? {}),
  });

export const api = {
  drivers: () => request<{ drivers: Driver[] }>("/api/drivers").then((r) => r.drivers),
  saveDriver: (driver: Partial<Driver>) =>
    driver.id
      ? write<Driver>(`/api/drivers/${driver.id}`, driver, "PATCH")
      : write<Driver>("/api/drivers", driver),
  removeDriver: (id: number) => write<{ ok: boolean }>(`/api/drivers/${id}`, {}, "DELETE"),
  driverBoard: () => request<{ areas: BoardArea[] }>("/api/drivers/board").then((r) => r.areas),
  driverLocation: (id: number, area: string) =>
    write<{ ok: boolean; error?: string }>(`/api/drivers/${id}/location`, { area }),
  driverFlash: (id: number) => write<{ status: string }>(`/api/drivers/${id}/flash`, {}),
  areas: () => request<{ areas: Area[] }>("/api/areas").then((r) => r.areas),
  saveArea: (area: Partial<Area>) => write<{ id: number }>("/api/areas", area),
  openTender: (
    orderId: number,
    body: { area?: string; filters?: TenderFilters; window_seconds?: number },
  ) => write<{ eligible: number; flash: number; voice: number }>(`/api/orders/${orderId}/tender`, body),
  tenders: () => request<{ tenders: Tender[] }>("/api/tenders").then((r) => r.tenders),
  closeTender: (id: number) => write<{ ok: boolean }>(`/api/tenders/${id}/close`, {}),
  cancelTender: (id: number) => write<{ ok: boolean }>(`/api/tenders/${id}/cancel`, {}),
  finishOrder: (id: number) => write<{ ok: boolean }>(`/api/orders/${id}/finish`, {}),
  cancelOrder: (id: number) => write<{ points_reversed: number }>(`/api/orders/${id}/cancel`, {}),
  redeemOrder: (id: number) => write<{ spent: number }>(`/api/orders/${id}/redeem`, {}),
  createOrder: (order: {
    phone: string;
    origin: string;
    destination: string;
    passengers?: number;
    pickup_time?: string;
    price?: number;
    notes?: string;
    tender?: boolean;
  }) => write<{ id: number }>("/api/orders", order),
  clubMembers: () =>
    request<{ members: ClubMember[] }>("/api/club/members").then((r) => r.members),
  clubMember: (phone: string) => request<ClubDetail>(`/api/club/${phone}`),
  adjustPoints: (phone: string, delta: number, note: string) =>
    write<{ balance: number }>(`/api/club/${phone}/adjust`, { delta, note }),
  savePreferences: (phone: string, prefs: Partial<ClubDetail["preferences"] & { name: string }>) =>
    write<{ ok: boolean }>(`/api/club/${phone}/preferences`, prefs, "PATCH"),
  referrals: () =>
    request<{ referrals: Referral[] }>("/api/referrals").then((r) => r.referrals),
  createReferral: (referrer_phone: string, invited_phone: string, flash: boolean) =>
    write<{ ok: boolean }>("/api/referrals", { referrer_phone, invited_phone, flash }),
  ratings: () =>
    request<{ ratings: RatingRequest[] }>("/api/ratings").then((r) => r.ratings),
  callRating: (id: number) => write<{ status: string }>(`/api/ratings/${id}/call`, {}),
  scoreRating: (id: number, score: number) =>
    write<{ ok: boolean }>(`/api/ratings/${id}/score`, { score }),
  books: (days: number) => request<Books>(`/api/accounting/summary?days=${days}`),
  driverRides: (days: number) =>
    request<{ drivers: DriverRides[] }>(`/api/accounting/drivers?days=${days}`).then(
      (r) => r.drivers,
    ),
  statement: (id: number, days: number) =>
    request<Statement>(`/api/accounting/drivers/${id}?days=${days}`),
  sendStatement: (id: number, days: number) =>
    write<Statement>(`/api/accounting/drivers/${id}/send`, { days }),
  expenses: () => request<{ expenses: Expense[] }>("/api/expenses").then((r) => r.expenses),
  addExpense: (category: string, amount: number, note: string) =>
    write<{ id: number }>("/api/expenses", { category, amount, note }),
  logs: (action?: string) =>
    request<{ logs: LogRow[] }>(`/api/logs${action ? `?action=${action}` : ""}`).then(
      (r) => r.logs,
    ),
  settings: () =>
    request<{ settings: Record<string, string> }>("/api/settings").then((r) => r.settings),
  saveSettings: (values: Record<string, string>) =>
    write<{ settings: Record<string, string> }>("/api/settings", values, "PUT").then(
      (r) => r.settings,
    ),
  summary: () => request<Summary>("/api/summary"),
  orders: () => request<{ orders: Order[] }>("/api/orders").then((r) => r.orders),
  updateOrder: (id: number, patch: Partial<Order>) =>
    request<Order>(`/api/orders/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  calls: () => request<{ calls: Call[] }>("/api/calls").then((r) => r.calls),
  call: (id: number) => request<CallDetail>(`/api/calls/${id}`),
  prices: () => request<{ prices: Price[] }>("/api/prices").then((r) => r.prices),
  customers: () =>
    request<{ customers: Customer[] }>("/api/customers").then((r) => r.customers),
  prompt: () => request<{ content: string }>("/api/prompt").then((r) => r.content),
  savePrompt: (content: string) =>
    request<{ content: string }>("/api/prompt", {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
};
