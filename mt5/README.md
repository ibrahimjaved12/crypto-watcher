# MT5 spike (#393)

Tests whether Exness demo candles reach Python on Linux through MetaTrader 5 under Wine
(`gmag11/metatrader5_vnc`). This is a spike for #393; the Linux/Wine setup for the long-running
gateway is decided after it.

1. Prerequisites: Docker, and an Exness demo MT5 account (login number, trading password, server
   name). Never commit any of them.
2. Start it:
   ```sh
   cd mt5
   cp .env.example .env   # then set MT5_VNC_PASSWORD
   docker compose up -d   # the first start downloads about 4 GB
   ```
3. Open `http://localhost:3000` and sign in with the browser-screen user and password from `.env`.
   In MT5 use File > Login to Trade Account with the demo details, then right-click Market Watch >
   Show All.
4. Run the check from the repo root and keep the output:
   ```sh
   pip install mt5linux
   python mt5/spike_check.py
   ```
5. Forex is closed from Friday evening to Sunday evening UTC: prices do not move then, and the
   clock check needs the market open.
6. Stop with `docker compose down` (the `config` folder keeps the login). Both ports are bound to
   loopback only; never publish 8001, which allows code execution inside the container.
