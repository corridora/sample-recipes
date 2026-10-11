<script setup>
import { ref, onMounted } from 'vue';
import { context, trace, SpanStatusCode } from '@opentelemetry/api';
const query = ref('');
const recipes = ref([]);
const selected = ref(null);
const loading = ref(false);
const error = ref('');
const status = ref(null);
const enrichment = ref('');
const tracer = trace.getTracer('sample-recipes-vue', '1.0.0');

async function action(name, url, options) {
  const span = tracer.startSpan(name);
  try {
    // Initiate fetch while this action's context is current. The RUM provider
    // creates the real CLIENT span and injects its traceparent.
    const promise = context.with(trace.setSpan(context.active(), span), () => fetch(url, options));
    const response = await promise;
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return await response.json();
  } catch (e) {
    span.recordException(e);
    span.setStatus({ code: SpanStatusCode.ERROR, message: e.message });
    throw e;
  } finally { span.end(); }
}
async function search() {
  error.value = '';
  try { recipes.value = (await action('recipes.search', `/api/recipes?q=${encodeURIComponent(query.value)}`)).items; }
  catch (e) { error.value = e.message; }
}
async function openRecipe(id) {
  loading.value = true;
  error.value = '';
  enrichment.value = '';
  try { selected.value = await action('recipes.open', `/api/recipes/${id}`); }
  catch (e) { error.value = e.message; }
  finally { loading.value = false; }
}
async function enrich() {
  const id = selected.value.id;
  enrichment.value = 'pending';
  try {
    await action('recipes.enrich', `/api/recipes/${id}/enrich`, { method: 'POST' });
    for (let i = 0; i < 12; i++) {
      await new Promise(resolve => setTimeout(resolve, 500));
      const result = await action('recipes.enrichment-status', `/api/recipes/${id}/enrichment`);
      enrichment.value = result.status === 'ready' ? result.summary : result.status;
      if (result.status === 'ready') return;
    }
    enrichment.value = 'Still processing; try again shortly.';
  } catch (e) { error.value = e.message; enrichment.value = 'failed'; }
}
onMounted(async () => { await search(); status.value = await action('recipes.status', '/api/demo/status'); });
</script>

<template>
  <div class="page">
    <header><a class="wordmark" href="/">the recipe table<span>GOOD FOOD, SHARED SIMPLY</span></a><span class="sample">CORRIDORA / RECIPES</span></header>
    <main>
      <section class="intro"><span class="eyebrow">A SMALL COLLECTION FOR EVERYDAY COOKING</span><h1>Something good<br>starts here.</h1><p>Find a favorite, gather the ingredients, and make it your own.</p></section>
      <form @submit.prevent="search" class="search"><label for="search">Find a recipe or ingredient</label><div><input id="search" v-model="query" placeholder="Try tomatoes or chickpeas"/><button type="submit">Search recipes ↗</button></div></form>
      <p v-if="error" role="alert" class="error">{{ error }}</p>
      <div class="workspace">
        <section class="collection" aria-label="Recipe collection"><div class="section-label"><h2>The collection</h2><span>{{ recipes.length }} RECIPES</span></div>
          <button v-for="recipe in recipes" :key="recipe.id" :data-testid="`recipe-${recipe.id}`" class="card" @click="openRecipe(recipe.id)" :aria-pressed="selected?.id === recipe.id">
            <img :src="`/api/recipes/${recipe.id}/image`" alt="Illustrated plate" width="600" height="350"/>
            <div><small>{{ recipe.minutes }} MINUTES · VEGETARIAN</small><h3>{{ recipe.name }}</h3><p>{{ recipe.description }}</p><span class="card-link">View recipe ↗</span></div>
          </button>
          <p v-if="!recipes.length">No recipes found. Try another ingredient.</p>
        </section>
        <aside class="detail" aria-live="polite" :aria-busy="loading">
          <p v-if="loading" class="loading">Gathering your ingredients…</p>
          <template v-else-if="selected"><span class="eyebrow">AT YOUR TABLE</span><h2 data-testid="recipe-title">{{ selected.name }}</h2><p>{{ selected.description }}</p>
            <div class="rating">★ {{ selected.reviews.rating }} <small>from {{ selected.reviews.count }} community reviews</small></div>
            <h3>Ingredients</h3><ul><li v-for="item in selected.ingredients_detail" :key="item.name">{{ item.name.replaceAll('-', ' ') }}<small v-if="item.pantry">PANTRY</small></li></ul>
            <h3>Make it</h3><ol><li v-for="step in selected.steps" :key="step">{{ step }}</li></ol>
            <div class="enrich"><h3>A little extra inspiration</h3><p>Ask our background kitchen worker to prepare recipe notes.</p><button data-testid="enrich" @click="enrich" :disabled="enrichment === 'pending'">{{ enrichment === 'pending' ? 'Preparing notes…' : 'Prepare recipe notes' }}</button><p data-testid="enrichment-status">{{ enrichment }}</p></div>
            <details><summary>Recipe request details</summary><dl><dt>Ingredient lookup</dt><dd data-testid="read-mode">{{ selected.read_mode }}</dd><dt>Server duration</dt><dd data-testid="duration">{{ selected.duration_ms }} ms</dd><dt>Trace</dt><dd data-testid="trace-id">{{ selected.trace_id }}</dd></dl></details>
          </template>
          <div v-else class="empty"><span>✳</span><h2>Pull up a chair.</h2><p>Choose a recipe to see what’s cooking.</p></div>
        </aside>
      </div>
    </main>
    <footer><span>Made to share. Built to observe.</span><span>Vue + FastAPI · <a href="https://github.com/corridora/sample-recipes">Open source</a></span></footer>
  </div>
</template>
