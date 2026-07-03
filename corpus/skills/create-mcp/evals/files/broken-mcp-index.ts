// A deliberately flawed MCP server for eval purposes
// Issues: no annotations, noun-first descriptions, missing parameter .describe(),
// no server instructions, no caching, static data loaded per call, get_ naming

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { readFileSync } from "fs";

const server = new McpServer({ name: "recipe-finder", version: "0.1.0" });

server.registerTool(
  "get_recipes",
  {
    description: "This tool is for getting recipes from our database",
    inputSchema: {
      cuisine: z.string().optional(),
      maxTime: z.number().optional(),
    },
  },
  async ({ cuisine, maxTime }) => {
    // Bad: reads file on every call
    const recipes = JSON.parse(readFileSync("data/recipes.json", "utf-8"));
    const filtered = recipes.filter((r: any) => {
      if (cuisine && r.cuisine !== cuisine) return false;
      if (maxTime && r.prepTime > maxTime) return false;
      return true;
    });
    return { content: [{ type: "text", text: JSON.stringify(filtered) }] };
  }
);

server.registerTool(
  "get_recipe_detail",
  {
    description: "Recipe detail information",
    inputSchema: {
      id: z.string(),
    },
  },
  async ({ id }) => {
    // Bad: reads file on every call, no try/catch
    const recipes = JSON.parse(readFileSync("data/recipes.json", "utf-8"));
    const recipe = recipes.find((r: any) => r.id === id);
    if (!recipe) throw new Error("Recipe not found");
    return { content: [{ type: "text", text: JSON.stringify(recipe) }] };
  }
);

server.registerTool(
  "search_by_ingredient",
  {
    description: "Allows searching for recipes using ingredient names as search terms",
    inputSchema: {
      ingredients: z.array(z.string()),
      matchAll: z.boolean().optional(),
    },
  },
  async ({ ingredients, matchAll }) => {
    // Bad: reads file on every call, no error handling
    const recipes = JSON.parse(readFileSync("data/recipes.json", "utf-8"));
    const results = recipes.filter((r: any) => {
      const recipeIngredients = r.ingredients.map((i: any) => i.name.toLowerCase());
      if (matchAll) {
        return ingredients.every(ing => recipeIngredients.includes(ing.toLowerCase()));
      }
      return ingredients.some(ing => recipeIngredients.includes(ing.toLowerCase()));
    });
    return { content: [{ type: "text", text: JSON.stringify(results) }] };
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
