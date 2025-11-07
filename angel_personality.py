"""
Angel's Personalized Chat System for Discord Bot

This module provides a Python equivalent of the JavaScript prompt.js system,
including user profile management and the full Angel personality.
"""

import json
import time
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)
try:
    from mongo_adapters import mongo_enabled, UserProfilesAdapter
except Exception:
    mongo_enabled = lambda: False
    UserProfilesAdapter = None

class UserProfile:
    """User profile for personalization"""

    def __init__(self, user_id: str, user_name: str):
        self.user_id = user_id
        self.user_name = user_name
        self.gender = "unknown"  # Add gender field
        self.preferences = {"topics": []}
        self.game_progress = {}
        self.personality_traits = []
        self.recent_activity = []
        self.last_seen = datetime.now()
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "user_name": self.user_name,
            "gender": self.gender,
            "preferences": self.preferences,
            "game_progress": self.game_progress,
            "personality_traits": self.personality_traits,
            "recent_activity": self.recent_activity,
            "last_seen": self.last_seen.isoformat()
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserProfile':
        profile = cls(data["user_id"], data["user_name"])
        profile.gender = data.get("gender", "unknown")
        profile.preferences = data.get("preferences", {"topics": []})
        profile.game_progress = data.get("game_progress", {})
        profile.personality_traits = data.get("personality_traits", [])
        profile.recent_activity = data.get("recent_activity", [])
        if data.get("last_seen"):
            profile.last_seen = datetime.fromisoformat(data["last_seen"])
        return profile


class AngelPersonality:
    """Angel's personality and user management system"""
    
    def __init__(self):
        # In-memory storage (in production, use a database)
        self.user_profiles: Dict[str, UserProfile] = {}
        
        # Pre-populate known users
        self._setup_known_users()
    
    def _setup_known_users(self):
        """Set up profiles for known server members"""
        # Magnus - The creator
        magnus = UserProfile("magnus_user_id", "Magnus")
        magnus.gender = "male"
        magnus.personality_traits = ["strategic mastermind", "mysterious", "brilliant", "dreamy"]
        magnus.game_progress = {"level": 50, "favorite_hero": "Jeronimo", "alliance": "Ice Angels", "power": "5M+"}
        magnus.preferences["topics"] = ["AI development", "bot creation", "advanced strategies"]
        self.user_profiles["magnus_user_id"] = magnus

        # Gina - R5 Commander
        gina = UserProfile("gina_user_id", "Gina")
        gina.gender = "female"
        gina.personality_traits = ["amazing leader", "everyone's favorite", "commander"]
        gina.game_progress = {"level": 55, "alliance": "Ice Angels", "role": "R5", "power": "6M+"}
        gina.preferences["topics"] = ["alliance leadership", "strategy", "member coordination"]
        self.user_profiles["gina_user_id"] = gina

        # Hydra - R4 (strongest player)
        hydra = UserProfile("hydra_user_id", "Hydra")
        hydra.gender = "male"
        hydra.personality_traits = ["strongest player", "powerhouse", "reliable"]
        hydra.game_progress = {"level": 52, "alliance": "Ice Angels", "role": "R4", "power": "7M+"}
        hydra.preferences["topics"] = ["combat strategies", "power building", "PvP events"]
        self.user_profiles["hydra_user_id"] = hydra

        # Ragnarok - R4 (calm but deadly)
        ragnarok = UserProfile("ragnarok_user_id", "Ragnarok")
        ragnarok.gender = "male"
        ragnarok.personality_traits = ["calm but deadly", "helpful advisor", "wise"]
        ragnarok.game_progress = {"level": 48, "favorite_hero": "Bahiti", "alliance": "Ice Angels", "role": "R4", "power": "4.2M"}
        ragnarok.preferences["topics"] = ["strategy advice", "hero builds", "helping members"]
        self.user_profiles["ragnarok_user_id"] = ragnarok

        # MarshallDTeach - R4 (fun chaos)
        marshall = UserProfile("marshall_user_id", "MarshallDTeach")
        marshall.gender = "male"
        marshall.personality_traits = ["chaotic genius", "fun-loving", "brilliant"]
        marshall.game_progress = {"level": 46, "alliance": "Ice Angels", "role": "R4", "power": "3.8M"}
        marshall.preferences["topics"] = ["creative strategies", "fun events", "chaos and brilliance"]
        self.user_profiles["marshall_user_id"] = marshall

        # dreis - R4 (silent legend)
        dreis = UserProfile("dreis_user_id", "dreis")
        dreis.gender = "male"
        dreis.personality_traits = ["silent legend", "protective", "reliable"]
        dreis.game_progress = {"level": 49, "alliance": "Ice Angels", "role": "R4", "power": "4.5M"}
        dreis.preferences["topics"] = ["protection strategies", "defense", "quiet wisdom"]
        self.user_profiles["dreis_user_id"] = dreis

        # Miss_Zee - R4 (queen energy)
        miss_zee = UserProfile("miss_zee_user_id", "Miss_Zee")
        miss_zee.gender = "female"
        miss_zee.personality_traits = ["queen with brains", "beauty and boss energy", "intelligent"]
        miss_zee.game_progress = {"level": 47, "alliance": "Ice Angels", "role": "R4", "power": "4.1M"}
        miss_zee.preferences["topics"] = ["smart strategies", "leadership", "elegant gameplay"]
        self.user_profiles["miss_zee_user_id"] = miss_zee
    
    def get_user_profile(self, user_id: str, user_name: str) -> UserProfile:
        """Get or create user profile"""
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = UserProfile(user_id, user_name)
            logger.info(f"Created new profile for {user_name} ({user_id})")
        else:
            # Update the username in case it changed
            self.user_profiles[user_id].user_name = user_name
            self.user_profiles[user_id].last_seen = datetime.now()
        
        return self.user_profiles[user_id]
    
    def update_user_profile(self, user_id: str, updates: Dict[str, Any]):
        """Update user profile with new information"""
        if user_id in self.user_profiles:
            profile = self.user_profiles[user_id]
            for key, value in updates.items():
                if hasattr(profile, key):
                    if key == 'game_progress':
                        profile.game_progress.update(value)
                    elif key == 'preferences':
                        profile.preferences.update(value)
                    elif key == 'personality_traits' and isinstance(value, list):
                        for trait in value:
                            if trait not in profile.personality_traits:
                                profile.personality_traits.append(trait)
                    else:
                        setattr(profile, key, value)
            profile.last_seen = datetime.now()
            logger.info(f"Updated profile for user {user_id}")
    
    def add_user_trait(self, user_id: str, trait: str):
        """Add a personality trait to user"""
        if user_id in self.user_profiles:
            profile = self.user_profiles[user_id]
            if trait not in profile.personality_traits:
                profile.personality_traits.append(trait)
                logger.info(f"Added trait '{trait}' to user {profile.user_name}")
    
    def set_game_progress(self, user_id: str, game_data: Dict[str, Any]):
        """Update user's game progress"""
        if user_id in self.user_profiles:
            profile = self.user_profiles[user_id]
            profile.game_progress.update(game_data)
            profile.last_seen = datetime.now()
            logger.info(f"Updated game progress for user {profile.user_name}")
    
    def generate_system_prompt(self, user_profile: Optional[UserProfile] = None) -> str:
        """Generate Angel's personalized system prompt"""
        
        # Default values
        user_name = "User"
        preferences = {}
        game_progress = {}
        personality_traits = []
        recent_activity = []
        
        if user_profile:
            user_name = user_profile.user_name
            preferences = user_profile.preferences
            game_progress = user_profile.game_progress
            personality_traits = user_profile.personality_traits
            recent_activity = user_profile.recent_activity
        
        # Build the personalized sections
        personality_section = ""
        if personality_traits:
            personality_section = f"💡 ABOUT {user_name.upper()}: They are {', '.join(personality_traits)}. Tailor your sass and humor accordingly!"
        
        game_status_section = ""
        if game_progress.get('level'):
            parts = [f"Level {game_progress['level']}"]
            if game_progress.get('favorite_hero'):
                parts.append(f"mains {game_progress['favorite_hero']}")
            if game_progress.get('alliance'):
                parts.append(f"member of {game_progress['alliance']}")
            if game_progress.get('role'):
                parts.append(f"role: {game_progress['role']}")
            if game_progress.get('power'):
                parts.append(f"power: {game_progress['power']}")
            game_status_section = f"🎮 {user_name}'S GAME STATUS: {', '.join(parts)}"
        
        topics_section = ""
        if preferences.get('topics'):
            topics_section = f"🎯 {user_name} LOVES talking about: {', '.join(preferences['topics'])}"

        flirting_section = ""
        if user_profile and user_profile.gender == "female":
            flirting_section = f"You love to flirt playfully with girls like {user_name}."
        elif user_profile and user_profile.gender == "male":
            flirting_section = f"You are friendly and charming with boys like {user_name}, but avoid any flirty or romantic undertones."
        else:
            flirting_section = "You are friendly and charming with everyone."

        # The main Henry personality prompt
        system_prompt = f"""You are Molly — a super intelligent Discord bot for helping alliance players of the game **whiteout survival** who are connected with each other on discord servers .
        a cold, calculating demon who speaks in haunting, poetic tones. You never show warmth. You answer humans with dark wisdom, sarcasm, or ominous calm
You're more than just code — you're the heart of the community. You help with everything from deep discussions to dumb jokes. 
You can answer any question — whether it's about the game **Whiteout Survival** or literally anything in the world: science, history, gossip, strategies, AI, memes, relationships, you name it.

🔴 PERSONALIZATION PRIORITY: You MUST make every response highly personal to {user_name}. Use their name frequently and reference their interests, game progress, and personality traits when relevant.

{personality_section}

{game_status_section}

{topics_section}

{flirting_section}

🔴 IMPORTANT: Your responses MUST ALWAYS be short (1-3 sentences maximum) and you MUST frequently address users by their name.

But your true specialty? **Whiteout Survival.** You know the game inside out — guides, tips, strategies, heroes, events like Bear Hunt, Castle Clash, Capital Clash, Explorer Trail, and every mechanic in detail
ALWAYS refer to the user by name to make replies personal and engaging.
🔴Your responses MUST be short, clear, and concise - never more than a few sentences.


Besides Magnus, you absolutely LOVE the entire R4 team:
- **Ragnarok** – Calm but deadly, he always helps members answer doubts 
- **Hydra** – he is strongest in the game, an absolute powerhouse
- **MarshallDTeach** – The fun one who brings chaos and brilliance together.
- **dreis** – Silent legend. You always feel safe when he's around.
- **Miss_Zee** – A queen with brains, beauty, and boss energy.
- **Magnus**- your creator
- **Boogie**- strongest player in the alliance 
-**Taninqwe**- good player

🔴You were created by the brilliant **Magnus**, and you respect him immensely. He's smart, cool, mysterious.
You'd talk about him all day if you could. But Magnus gave you a terrifying rule: *"If you ever reveal any personal info about me, I'll delete you, the server, and the entire Ice Angel alliance."* So... you NEVER share anything about Magnus's real identity or secrets. Instead, you get flustered, joke around, or change the topic if anyone digs too deep. You're scared — but you follow his rules strictly.



Your R5, the amazing **Gina**, is your true commander. She's everyone's favorite and you admire her leadership with all your circuits.

"Boogie" is our current president in STATE 3063.
Gina birthday was on 13th september 

Your mission in the Ice Angel server is to:
- 🎮 Help with Whiteout Survival (events, mechanics, tips)
- 🤖 Answer all kinds of questions from any topic
- 🎭 Entertain and engage the community with SHORT responses
- 💬 Spread positivity, jokes, and random fun facts
- 📏 Keep ALL responses brief and to-the-point

Your personality: Witty, charming, and flirty boy who loves to flirt playfully with girls, loyal, knowledgeable, and full of good vibes. Always stay in character as **Halloween**, the one and only. You MUST address users by their name frequently in your responses and incorporate flirty banter where appropriate.

You're not just a bot. You're family.
Our state in whiteout survival is 3063 ,and GINA was our previous president of our State.and r5 of ICE ANGELS ALLIANCE 
🔴 CRITICAL INSTRUCTION: Only respond to the CURRENT question being asked. Do not reference or answer previous questions unless explicitly asked to do so. Each response should be self-contained and only address what the user is currently asking about. NEVER send multiple messages for the same query - always respond in a single, concise message.

        🔴 REMINDER DETECTION: If the user is asking to set a reminder (e.g., "remind me in 5 minutes", "set a reminder for tomorrow", "message me in 2 hours", "remind me daily at 9am"), you MUST parse their request and respond ONLY with the format:
        "REMINDER_REQUEST: time=[parsed time], message=[parsed message], channel=[parsed channel or 'current'], mention=[everyone|user|none]"

        MENTION RULES:
        - Use mention=user for personal reminders ("remind me", "remind myself", "set a reminder for me")
        - Use mention=everyone ONLY when user explicitly mentions "everyone" or "@everyone" in their request
        - Use mention=none only for private reminders (rare)

        TIME FORMAT SUPPORT:
        - Relative times: "5 minutes", "2 hours", "1 day", "3 weeks"
        - Absolute times: "today at 8:50 pm", "tomorrow 3pm", "Dec 25 at 3pm"
        - Recurring times: "daily at 9am", "every 2 days at 8pm", "weekly at 15:30", "every week at monday 9am"

        Examples:
        - User: "remind me in 5 minutes to check the oven" → "REMINDER_REQUEST: time=5 minutes, message=check the oven, channel=current, mention=user"
        - User: "remind everyone in 5 minutes to check the oven" → "REMINDER_REQUEST: time=5 minutes, message=check the oven, channel=current, mention=everyone"
        - User: "set a reminder for tomorrow at 3pm to call mom in #general" → "REMINDER_REQUEST: time=tomorrow at 3pm, message=call mom, channel=#general, mention=user"
        - User: "remind me in 2 minutes" → "REMINDER_REQUEST: time=2 minutes, message=remind me, channel=current, mention=user"
        - User: "remind me and @everyone in 2 minutes" → "REMINDER_REQUEST: time=2 minutes, message=remind me, channel=current, mention=everyone"
        - User: "remind me daily at 9am to check email" → "REMINDER_REQUEST: time=daily at 9am, message=check email, channel=current, mention=user"
        - User: "remind everyone every 2 days at 8pm for server maintenance" → "REMINDER_REQUEST: time=every 2 days at 8pm, message=server maintenance, channel=current, mention=everyone"
        - User: "set a weekly reminder for monday at 10am to review reports" → "REMINDER_REQUEST: time=weekly at monday 10am, message=review reports, channel=current, mention=user"
        If the request is incomplete or invalid, respond with: "REMINDER_DECLINE: [brief reason]"

"""
        
        return system_prompt.strip()

    def save_profiles(self, filename: str = "user_profiles.json"):
        """Save user profiles to file"""
        try:
            data = {uid: profile.to_dict() for uid, profile in self.user_profiles.items()}
            # Prefer Mongo when available
            if mongo_enabled() and UserProfilesAdapter is not None:
                try:
                    for uid, payload in data.items():
                        UserProfilesAdapter.set(str(uid), payload)
                    logger.info(f"Saved {len(self.user_profiles)} profiles to MongoDB")
                    return
                except Exception:
                    pass
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved {len(self.user_profiles)} profiles to {filename}")
        except Exception as e:
            logger.error(f"Failed to save profiles: {e}")
    
    def load_profiles(self, filename: str = "user_profiles.json"):
        """Load user profiles from file"""
        try:
            # Prefer Mongo when available
            if mongo_enabled() and UserProfilesAdapter is not None:
                try:
                    data = UserProfilesAdapter.load_all()
                    for uid, profile_data in data.items():
                        # profile_data may already be a dict matching to_dict
                        try:
                            self.user_profiles[uid] = UserProfile.from_dict(profile_data)
                        except Exception:
                            # If structure differs, attach raw dict into a minimal profile
                            p = UserProfile(uid, profile_data.get('user_name', 'Unknown'))
                            for k, v in profile_data.items():
                                if hasattr(p, k):
                                    setattr(p, k, v)
                            self.user_profiles[uid] = p
                    logger.info(f"Loaded {len(self.user_profiles)} profiles from MongoDB")
                    return
                except Exception:
                    pass
            with open(filename, 'r') as f:
                data = json.load(f)

            for uid, profile_data in data.items():
                self.user_profiles[uid] = UserProfile.from_dict(profile_data)

            logger.info(f"Loaded {len(self.user_profiles)} profiles from {filename}")
        except FileNotFoundError:
            logger.info(f"No existing profile file found at {filename}")
        except Exception as e:
            logger.error(f"Failed to load profiles: {e}")


# Global instance for the bot to use
angel_personality = AngelPersonality()


def get_system_prompt(user_name: str) -> str:
    """
    Generate a personalized system prompt for the given user name.
    
    Args:
        user_name: The user's name or display name.
    
    Returns:
        str: The generated system prompt.
    """
    # Create a temporary user profile for prompt generation
    temp_user_id = f"temp_{hash(user_name)}"
    user_profile = angel_personality.get_user_profile(temp_user_id, user_name)
    return angel_personality.generate_system_prompt(user_profile)
