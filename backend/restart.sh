#!/bin/bash

PROJECT=/home/mo/codes/project/universal-kb

echo "[1] 停止后端..."
pkill -9 -f 'uvicorn app.main:app' 2>/dev/null && echo "  后端已停止" || echo "  后端未运行"

echo "[2] 停止前端..."
pkill -9 -f 'next-server' 2>/dev/null && echo "  前端已停止" || echo "  前端未运行"
pkill -9 -f 'next dev' 2>/dev/null || true

echo "[3] 等待端口释放..."
sleep 3

echo "[4] 启动后端 (端口 8001)..."
cd $PROJECT/backend
DISABLE_ASR_PRELOAD=1 nohup /home/mo/anaconda3/bin/uvicorn app.main:app --port 8001 --host 0.0.0.0 > /tmp/uvicorn.log 2>&1 &
echo "  后端 PID: $!"

echo "[5] 启动前端..."
cd /home/mo/codes/project/le-desk
BACKEND_URL=http://localhost:8001 nohup npm run dev > /tmp/le-desk.log 2>&1 &
echo "  前端 PID: $!"

echo "[6] 等待服务启动..."
sleep 8

echo ""
echo "=== 进程状态 ==="
ps aux | grep -E 'uvicorn|next-server' | grep -v grep | head -5

echo ""
echo "=== 后端健康检查 ==="
curl -s http://localhost:8001/api/auth/login -X POST -H 'Content-Type: application/json' -d '{"username":"admin","password":"admin123"}' | head -c 100

echo ""
echo ""
echo "=== 完成 ==="
echo "前端：http://8.134.184.254:5023"
echo "后端：http://8.134.184.254:8001/docs"
