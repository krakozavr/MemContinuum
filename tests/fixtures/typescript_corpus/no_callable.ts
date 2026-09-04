export interface Config {
  retries: number;
  timeout: number;
}
export const DEFAULT_CONFIG: Config = { retries: 3, timeout: 1000 };
