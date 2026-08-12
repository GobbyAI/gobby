/** @type {import('prettier').Config} */
export default {
  plugins: ["prettier-plugin-tailwindcss"],
  tailwindStylesheet: "./src/styles/index.css",
  tailwindFunctions: ["cn", "clsx", "cva", "twMerge"],
  tailwindPreserveDuplicates: true,
};
