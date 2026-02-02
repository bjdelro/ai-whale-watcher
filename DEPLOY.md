# Deploy AI Whale Watcher (Free/Cheap Options)

## Option 1: Fly.io (FREE - Best Option)
**Cost: $0** (free tier includes 3 shared VMs)

```bash
# 1. Install Fly CLI
curl -L https://fly.io/install.sh | sh

# 2. Login/signup
fly auth login

# 3. Create app and volume for data
fly launch --no-deploy
fly volumes create whale_data --size 1 --region ewr

# 4. Set your Polymarket private key
fly secrets set PRIVATE_KEY=your_private_key_here

# 5. Deploy
fly deploy

# 6. Check logs
fly logs

# 7. SSH in to check data
fly ssh console
cat /app/market_logs/activity_*.jsonl
```

---

## Option 2: Railway (FREE $5/month credit)
**Cost: $0** (free tier, no credit card needed)

1. Go to https://railway.app
2. Click "Start a New Project"
3. Select "Deploy from GitHub repo"
4. Connect your repo
5. Add environment variable: `PRIVATE_KEY=your_key`
6. Deploy automatically

---

## Option 3: Render (FREE for 90 days)
**Cost: $0** (free trial, then $7/month)

1. Go to https://render.com
2. New → Worker
3. Connect GitHub repo
4. Add `PRIVATE_KEY` env var
5. Deploy

---

## Option 4: Run on Any VPS ($4-5/month)
**DigitalOcean, Vultr, or Hetzner**

```bash
# SSH into your server
ssh root@your-server-ip

# Install Docker
curl -fsSL https://get.docker.com | sh

# Clone repo
git clone https://github.com/YOUR_USERNAME/ai-whale-watcher.git
cd ai-whale-watcher

# Create .env file
echo "PRIVATE_KEY=your_key_here" > .env

# Run with Docker
docker build -t whale-watcher .
docker run -d --restart always \
  --name whale-watcher \
  -v $(pwd)/market_logs:/app/market_logs \
  whale-watcher

# Check logs
docker logs -f whale-watcher
```

---

## Quick Start (Fly.io Recommended)

```bash
cd ~/Desktop/ai-whale-watcher

# Install fly CLI
brew install flyctl  # or: curl -L https://fly.io/install.sh | sh

# Login (creates free account)
fly auth signup

# Launch
fly launch --name ai-whale-watcher --no-deploy

# Create storage volume
fly volumes create whale_data --size 1

# Set your private key
fly secrets set PRIVATE_KEY=0xa8ef1ac6...your_full_key

# Deploy!
fly deploy

# Watch logs
fly logs --app ai-whale-watcher
```

Your PRIVATE_KEY from .env: Check with `cat .env | grep PRIVATE_KEY`
