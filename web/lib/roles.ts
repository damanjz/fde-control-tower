// The three people the tower knows (tower/access.py). Ids only: tokens never leave the server.
export const ROLES = [
  { id: "asha", name: "Asha", title: "VP Operations", av: "#253041" },
  { id: "kiran", name: "Kiran", title: "Dispatcher South", av: "#1f4fa8" },
  { id: "meera", name: "Meera", title: "Finance", av: "#1d6b4a" },
] as const;
export type RoleId = (typeof ROLES)[number]["id"];
export const ROLE_COOKIE = "ct_role";
export const ROLE_HEADER = "x-ct-role";
export const STATUS_HEADER = "x-tower-status";
export const isRole = (v: unknown): v is RoleId => ROLES.some((r) => r.id === v);
export const roleOf = (id: RoleId) => ROLES.find((r) => r.id === id)!;
