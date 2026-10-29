import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发时由 Vite 转发请求，前端不需要接触模型密钥，也不用修改后端跨域配置。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
});
