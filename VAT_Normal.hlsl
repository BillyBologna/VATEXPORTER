// Material Expression "Custom" - Output Type: CMOT Float 3 - Description: VAT_Normal
// Inputs (nombres exactos):
//   NrmTex  -> Texture Object (tu textura ..._nrm.exr)
//   UV2, Frame, Frames, TexW, Tiles -> los mismos que en VAT_Position
// Salida: normal VAT en espacio local.
float f  = fmod(Frame, Frames);
float f0 = floor(f);
float f1 = fmod(f0 + 1.0, Frames);
float t  = f - f0;

float tile = floor(UV2.x * Tiles + 0.5);
float2 uvA = float2((tile * Frames + f0 + 0.5) / TexW, UV2.y);
float2 uvB = float2((tile * Frames + f1 + 0.5) / TexW, UV2.y);

float3 nA = Texture2DSampleLevel(NrmTex, NrmTexSampler, uvA, 0).xyz;
float3 nB = Texture2DSampleLevel(NrmTex, NrmTexSampler, uvB, 0).xyz;
return normalize(lerp(nA, nB, t));
