# CS 457 Project- Knight 1v1 Duel

**Student Name:** Makaela Maryanski

**Date:** 2026-09-16

**Course:** CS 457 - Computer Networks

**Target Server Domain:** `server.maryanski.edu`

---


### 1.1 Game Overview
- **Chosen Game:** Knight Duel (1v1 Tactical Combat)
- **Player Capacity:** 2 Players (Simulated via 2 CML Client nodes)
- **Game Summary:** A turn-based tactical combat game using classic Pokémon-style battle mechanics for a head-to-head Knight fight. Two players command a Knight with base stats (HP, Stamina, Speed) and 4 core actions: Sword Attack (higerh energy/stamina cost but higher damage), Bow Attack (lower speed/stamina cost but lower damage), Block (reduces incoming damage with chance to stun), and Prepare (recovers HP and boosts Speed for future turns).

### 1.2 Core Game Rules & Win/Draw Conditions
- **Turn Mechanics:** Both players submit their move choice during a selection phase. The server collects both actions and resolves execution order based on current Knight Speed stats (faster Knight acts first). The server calculates damage, checks for Block, applies stat modifications or healing, and broadcasts the combat log and updated HP meters to both clients via `STATE_UPDATE`.
- **Victory Condition:** A player wins when the opposing Knight's Health Points (HP) drops to 0.
- **Draw/Tie Condition:** If both Knights drop to 0 HP on the same turn or if 30 battle rounds elapse without a knockout, the game ends in a draw.
