const API_BASE = process.env.API_BASE || "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function apiFetch(
  path: string,
  options: RequestInit & { token?: string } = {},
) {
  const { token, ...fetchOptions } = options;
  const headers = new Headers(fetchOptions.headers);

  if (!(fetchOptions.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const resp = await fetch(`${API_BASE}${path}`, { ...fetchOptions, headers });

  if (!resp.ok) {
    const body = await resp.text();
    throw new ApiError(resp.status, body);
  }

  const text = await resp.text();
  return text ? JSON.parse(text) : null;
}

export { API_BASE };
