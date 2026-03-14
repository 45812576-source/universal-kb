import { Welcome } from "../welcome/welcome";

export function meta({}: { params: Record<string, string> }) {
  return [
    { title: "New React Router App" },
    { name: "description", content: "Welcome to React Router!" },
  ];
}

export default function Home() {
  return <Welcome />;
}
