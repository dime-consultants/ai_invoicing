# chat/title_generator.py
"""
Auto-generate conversation titles based on chat messages using Grok AI.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def generate_conversation_title(messages: list[dict], max_retries: int = 2) -> Optional[str]:
    """
    Generate a concise, descriptive title for a conversation based on its messages.
    
    Args:
        messages: List of message dicts with 'role' and 'content' keys
        max_retries: Number of retries if the AI call fails
        
    Returns:
        A generated title string, or None if generation fails
    """
    if not messages:
        return None
    
    try:
        from ai_engine.services import _get_client, GROK_MODEL
    except ImportError:
        logger.warning("Grok client not available - cannot generate title")
        return None
    
    # Extract first few messages for context
    context_messages = messages[:6]  # Use first 6 messages for context
    
    # Build a summary of the conversation
    summary = "\n".join([
        f"{msg.get('role', 'unknown').upper()}: {msg.get('content', '')[:200]}"
        for msg in context_messages
    ])
    
    system_prompt = """
You are an expert at creating concise, descriptive titles for conversations.
Generate a short title (3-7 words) that captures the main topic or intent of the conversation.
Return ONLY the title text, nothing else. No quotes, no explanation.
"""
    
    user_prompt = f"""
Based on this conversation, generate a concise title:

{summary}

Title:"""
    
    for attempt in range(max_retries):
        try:
            client = _get_client()
            response = client.chat.completions.create(
                model=GROK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,  # Lower temperature for more consistent results
                max_tokens=50,
            )
            
            title = response.choices[0].message.content.strip()
            
            # Clean up the title
            title = title.strip('"\'')  # Remove quotes if present
            title = title.split('\n')[0]  # Take first line only
            
            if title and len(title) > 0:
                return title
        
        except Exception as exc:
            logger.warning(f"Title generation attempt {attempt + 1} failed: {exc}")
            if attempt == max_retries - 1:
                logger.error(f"Failed to generate title after {max_retries} attempts")
                return None
    
    return None


def generate_title_from_user_input(user_input: str) -> Optional[str]:
    """
    Generate a title based on the initial user input/prompt.
    
    Args:
        user_input: The first user message in the conversation
        
    Returns:
        A generated title string, or None if generation fails
    """
    if not user_input or len(user_input.strip()) == 0:
        return None
    
    # For very short inputs, just use them as the title
    if len(user_input) <= 50:
        return user_input.strip()
    
    try:
        from ai_engine.services import _get_client, GROK_MODEL
    except ImportError:
        logger.warning("Grok client not available - using input truncation")
        return user_input[:50].strip()
    
    system_prompt = """
You are an expert at creating concise, descriptive titles for conversations.
Generate a short title (3-7 words) that captures the main topic or intent based on the user's initial input.
Return ONLY the title text, nothing else. No quotes, no explanation.
"""
    
    user_prompt = f"""
Based on this user input, generate a concise title:

{user_input[:300]}

Title:"""
    
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=GROK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=50,
        )
        
        title = response.choices[0].message.content.strip()
        title = title.strip('"\'')
        title = title.split('\n')[0]
        
        if title and len(title) > 0:
            return title
    
    except Exception as exc:
        logger.warning(f"Title generation from user input failed: {exc}")
    
    # Fallback: truncate the user input
    return user_input[:50].strip()
