import { createCookieSessionStorage, redirect } from "react-router";
import type { User } from "./types";

const sessionStorage = createCookieSessionStorage({
  cookie: {
    name: "kb_session",
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    maxAge: 60 * 60 * 8, // 8 hours
    secrets: [process.env.SESSION_SECRET || "dev-secret-change-in-prod"],
  },
});

export async function getSession(request: Request) {
  return sessionStorage.getSession(request.headers.get("Cookie"));
}

export async function requireUser(
  request: Request,
): Promise<{ token: string; user: User }> {
  const session = await getSession(request);
  const token = session.get("token") as string | undefined;
  const user = session.get("user") as User | undefined;
  if (!token || !user) {
    throw redirect("/login");
  }
  return { token, user };
}

export async function createUserSession(token: string, user: User) {
  const session = await sessionStorage.getSession();
  session.set("token", token);
  session.set("user", user);
  return redirect("/", {
    headers: { "Set-Cookie": await sessionStorage.commitSession(session) },
  });
}

export async function destroyUserSession(request: Request) {
  const session = await getSession(request);
  return redirect("/login", {
    headers: { "Set-Cookie": await sessionStorage.destroySession(session) },
  });
}
