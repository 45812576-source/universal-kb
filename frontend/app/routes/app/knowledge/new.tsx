// knowledge/new redirects to knowledge/my (form is now embedded there)
import { redirect } from "react-router";

export function loader() {
  return redirect("/knowledge/my");
}

export default function NewKnowledgeRedirect() {
  return null;
}
