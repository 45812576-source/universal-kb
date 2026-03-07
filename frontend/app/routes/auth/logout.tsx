import type { Route } from "./+types/logout";
import { destroyUserSession } from "~/lib/auth.server";

export async function action({ request }: Route.ActionArgs) {
  return destroyUserSession(request);
}

export async function loader() {
  return null;
}

export default function Logout() {
  return null;
}
