from supabase_storage import SupabaseStorage # તમારી ફાઈલનું નામ જે રાખ્યું હોય તે

# 1. કનેક્શન ચેક કરવા માટે ઓબ્જેક્ટ બનાવો
db = SupabaseStorage()

print("Testing connection...")

# 2. નવું સેશન શરૂ કરી જુઓ
try:
    session_id = db.start_session()
    print(f"Success! નવું સેશન ID મળ્યું: {session_id}")
    print("તમારા Supabase ડેશબોર્ડમાં 'sessions' ટેબલ ચેક કરો.")
except Exception as e:
    print(f"Error આવી: {e}")


