// Material Expression "Custom" - Output Type: CMOT Float 3 - Description: VAT_Position
// Inputs (nombres exactos):
//   PosTex  -> Texture Object (tu textura ..._pos.exr)
//   UV2     -> TextureCoordinate (Coordinate Index = 1)   (Float2)
//   Frame   -> (Time * Fps * Speed + PerInstanceRandom * Frames)   (Float)
//   Frames  -> constante "frames" del JSON
//   TexW    -> constante texture[0] del JSON
//   Tiles   -> constante "tiles" del JSON
// Salida: posicion VAT en espacio local (delta si exportaste en modo Offset).
float f  = fmod(Frame, Frames);
float f0 = floor(f);
float f1 = fmod(f0 + 1.0, Frames);
float t  = f - f0;

float tile = floor(UV2.x * Tiles + 0.5);
float2 uvA = float2((tile * Frames + f0 + 0.5) / TexW, UV2.y);
float2 uvB = float2((tile * Frames + f1 + 0.5) / TexW, UV2.y);

float3 pA = Texture2DSampleLevel(PosTex, PosTexSampler, uvA, 0).xyz;
float3 pB = Texture2DSampleLevel(PosTex, PosTexSampler, uvB, 0).xyz;
return lerp(pA, pB, t);
