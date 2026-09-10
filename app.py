import os
import re
import pandas as pd
import numpy as np
from scipy.stats import poisson
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# Глобальные данные
matches_df = pd.DataFrame(columns=['date','team1','team2','team1_score','team2_score','total_goals'])

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Cyber Hockey Bot</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: linear-gradient(135deg, #1e3c72, #2a5298); color: white; padding: 20px; }
        .container { max-width: 500px; margin: 0 auto; }
        h1 { text-align: center; }
        .card { background: rgba(255,255,255,0.15); border-radius: 16px; padding: 20px; margin-bottom: 15px; }
        input, textarea, button { width: 100%; padding: 12px; border-radius: 8px; border: none; margin: 5px 0; font-size: 16px; box-sizing: border-box; }
        input, textarea { background: rgba(255,255,255,0.9); color: #333; }
        button { background: #4CAF50; color: white; font-weight: bold; cursor: pointer; }
        .result { white-space: pre-wrap; font-family: monospace; font-size: 14px; margin-top: 10px; }
        .status { margin-top: 10px; }
        label { display: block; margin-top: 10px; }
        .upload-zone { border: 2px dashed rgba(255,255,255,0.5); border-radius: 10px; padding: 30px; text-align: center; cursor: pointer; margin-bottom: 10px; }
        .hidden { display: none; }
    </style>
</head>
<body>
<div class="container">
    <h1>🏒 Cyber Hockey Bot</h1>
    <div class="card">
        <h3>📥 Загрузка данных</h3>
        <div class="upload-zone" onclick="document.getElementById('fileInput').click()">
            <p>Нажмите, чтобы загрузить CSV файл</p>
        </div>
        <input type="file" id="fileInput" accept=".csv" class="hidden" onchange="uploadFile()">
        <label>Или добавьте матч вручную (формат: Команда1 - Команда2 3:2 (1:1,1:0,1:1)):</label>
        <textarea id="manualData" rows="3" placeholder="Динамо - Спартак 3:2 (1:1,1:0,1:1)"></textarea>
        <button onclick="addManualData()">Добавить</button>
        <div class="status" id="dataStatus"></div>
    </div>
    <div class="card">
        <h3>🔮 Прогноз на матч</h3>
        <input type="text" id="team1" placeholder="Команда 1">
        <input type="text" id="team2" placeholder="Команда 2">
        <input type="number" id="odds" placeholder="Коэффициент (напр. 1.85)" step="0.01" value="1.85">
        <button onclick="predict()">Получить прогноз</button>
        <div class="result" id="result"></div>
    </div>
</div>
<script>
async function uploadFile() {
    const file = document.getElementById('fileInput').files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    document.getElementById('dataStatus').innerText = '⏳ Загрузка...';
    const resp = await fetch('/upload', {method: 'POST', body: formData});
    const data = await resp.json();
    document.getElementById('dataStatus').innerText = data.success ? '✅ ' + data.message : '❌ ' + data.error;
}

async function addManualData() {
    const text = document.getElementById('manualData').value;
    const resp = await fetch('/add_data', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({data: text})
    });
    const data = await resp.json();
    document.getElementById('dataStatus').innerText = data.success ? '✅ ' + data.message : '❌ ' + data.error;
    document.getElementById('manualData').value = '';
}

async function predict() {
    const team1 = document.getElementById('team1').value;
    const team2 = document.getElementById('team2').value;
    const odds = parseFloat(document.getElementById('odds').value);
    const resp = await fetch('/predict', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({team1, team2, odds})
    });
    const data = await resp.json();
    document.getElementById('result').innerText = data.success ? data.prediction : 'Ошибка: ' + data.error;
}
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Файл не выбран'})
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Пустое имя файла'})
    try:
        df = pd.read_csv(file)
        required = ['team1','team2','team1_score','team2_score']
        if not all(col in df.columns for col in required):
            return jsonify({'success': False, 'error': 'CSV должен содержать столбцы: team1, team2, team1_score, team2_score'})
        if 'total_goals' not in df.columns:
            df['total_goals'] = df['team1_score'] + df['team2_score']
        if 'date' not in df.columns:
            df['date'] = pd.Timestamp.now().strftime('%Y-%m-%d')
        global matches_df
        matches_df = pd.concat([matches_df, df[['date','team1','team2','team1_score','team2_score','total_goals']]], ignore_index=True)
        return jsonify({'success': True, 'message': f'Добавлено {len(df)} матчей. Всего: {len(matches_df)}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/add_data', methods=['POST'])
def add_data():
    data_text = request.json.get('data', '')
    if not data_text:
        return jsonify({'success': False, 'error': 'Пустые данные'})
    rows = []
    pattern = r'([А-Яа-яA-Za-z\s]+)\s*[-–]\s*([А-Яа-яA-Za-z\s]+)\s+(\d+):(\d+)'
    for line in data_text.strip().split('\n'):
        m = re.search(pattern, line)
        if m:
            team1 = m.group(1).strip()
            team2 = m.group(2).strip()
            score1 = int(m.group(3))
            score2 = int(m.group(4))
            rows.append({
                'date': pd.Timestamp.now().strftime('%Y-%m-%d'),
                'team1': team1,
                'team2': team2,
                'team1_score': score1,
                'team2_score': score2,
                'total_goals': score1 + score2
            })
    if not rows:
        return jsonify({'success': False, 'error': 'Не удалось распознать матчи. Проверьте формат.'})
    df = pd.DataFrame(rows)
    global matches_df
    matches_df = pd.concat([matches_df, df], ignore_index=True)
    return jsonify({'success': True, 'message': f'Добавлено {len(df)} матчей. Всего: {len(matches_df)}'})

def get_team_stats(team):
    """Возвращает статистику команды из глобального датасета"""
    team_matches = matches_df[(matches_df['team1'] == team) | (matches_df['team2'] == team)]
    if len(team_matches) == 0:
        return None
    return {
        'games': len(team_matches),
        'avg_total': team_matches['total_goals'].mean(),
        'home_total': team_matches[team_matches['team1'] == team]['total_goals'].mean(),
        'away_total': team_matches[team_matches['team2'] == team]['total_goals'].mean(),
    }

@app.route('/predict', methods=['POST'])
def predict():
    team1 = request.json.get('team1', '').strip()
    team2 = request.json.get('team2', '').strip()
    odds = float(request.json.get('odds', 1.85))
    if not team1 or not team2:
        return jsonify({'success': False, 'error': 'Введите обе команды'})
    if len(matches_df) < 10:
        return jsonify({'success': False, 'error': 'Недостаточно данных (минимум 10 матчей)'})

    stats1 = get_team_stats(team1)
    stats2 = get_team_stats(team2)
    if not stats1 or not stats2:
        return jsonify({'success': False, 'error': f'Одна из команд не найдена в базе ({team1} или {team2})'})
    
    avg_total = matches_df['total_goals'].mean()
    expected_total = (stats1['avg_total'] * 0.4 + stats2['avg_total'] * 0.4 + avg_total * 0.2)
    
    if stats1['home_total'] and not np.isnan(stats1['home_total']):
        expected_total = (expected_total + stats1['home_total']) / 2
    if stats2['away_total'] and not np.isnan(stats2['away_total']):
        expected_total = (expected_total + stats2['away_total']) / 2
    
    prob_over_55 = 1 - poisson.cdf(5, expected_total)
    
    value = prob_over_55 * odds - 1
    recommendation = ""
    if value > 0.05:
        b = odds - 1
        p = prob_over_55
        q = 1 - p
        kelly = (b * p - q) / b
        stake = max(0, kelly * 0.25 * 10000)
        recommendation = f"✅ VALUE BET! Ставка: {stake:.0f}₽ (на основе банка 10000₽)"
    else:
        recommendation = "❌ Нет value по текущему коэффициенту"
    
    per1 = expected_total * 0.30
    per2 = expected_total * 0.35
    per3 = expected_total * 0.35
    
    result = f"""
🏒 {team1} vs {team2}
=========================
⚽ Ожидаемый тотал: {expected_total:.2f}
📊 Вероятность ТБ 5.5: {prob_over_55*100:.1f}%
💰 Текущий коэффициент: {odds}
📈 Value: {value*100:.1f}%

⏱ Прогноз по периодам:
P1: {per1:.2f} гола
P2: {per2:.2f} гола
P3: {per3:.2f} гола

💡 Рекомендация: {recommendation}
"""
    return jsonify({'success': True, 'prediction': result})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
