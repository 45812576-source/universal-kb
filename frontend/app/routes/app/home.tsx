import { redirect } from "react-router";
import { requireUser } from "~/lib/auth.server";

export async function loader({ request }: { request: Request }) {
  await requireUser(request);
  throw redirect("/chat");
}

export default function Home() {
  return null;
}
